"""The questions and answers a guild plays from."""


from collections.abc import Iterable
import logging
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.utils.translation import gettext as _ 
from discordcore.cache import LIST_TIMEOUT, remember
from discordcore.members import DiscordMember, can_manage_guild
from discordcore.models import Guild, Player

from .. import caching
from .. import matching
from ..constants import (EDITABLE_FIELDS, LIBRARY_PAGE_SIZE, MAX_CHOICES,
                         MAX_YEAR)
from ..models import (Answer, AnswerVariant, Game, Question, QuizType,
                      question_problem)
from .guilds import player_of, require_host

logger = logging.getLogger(__name__)


def add_answer(guild: Guild, host_member: DiscordMember, text: str) -> Answer:
    """Return the guild's answer for this text, creating it when missing."""
    require_host(guild, host_member)
    text = text.strip()
    if not text:
        raise ValueError(_("Give the answer a text."))
    answer, created = Answer.objects.get_or_create(
        guild=guild, text__iexact=text, defaults={'text': text})
    if created:
        logger.info('%s: answer %s created', guild, text)
    return answer


def question_line(question: Question, *, with_answer: bool = False) -> str:
    """Return how a host identifies a question: its prompt, or its answer.
    With ``with_answer`` the answer is appended when the question has a prompt
    to show next to it; a prompt-less blind test question is its answer.
    """
    prompt = question.prompt.strip()
    if not prompt or not with_answer:
        return prompt or question.answer_text
    return f'{prompt} — answer: {question.answer_text}'


def split_answers(text: str) -> tuple[str, list[str]]:
    """Return the answer of a text and its variants, separated by ``|``."""
    parts = [part.strip() for part in str(text).split('|')]
    canonical = next((part for part in parts if part), '')
    return canonical, [part for part in parts if part and part != canonical]


def media_link(text: str) -> str:
    """Return a media link, refusing anything that is not a full URL."""
    url = text.strip()
    parts = urlsplit(url)
    if url and (parts.scheme not in ('http', 'https') or not parts.netloc):
        raise ValueError(_("Give the media link as a full http:// or https:// "
                           "URL."))
    return url


def clean_year(value: int | None | str) -> int | None:
    """Return a storable year, refusing a value the question cannot keep."""
    year = str(value).strip() if value is not None else ''
    if not year:
        return None
    if not (year.isdigit() and 1 <= int(year) <= MAX_YEAR):
        raise ValueError(_("Give the year as a number between 1 and %s.")
                         % MAX_YEAR)
    return int(year)


def visible_answer(guild: Guild, text: str) -> Answer:
    """Return the guild's answer for this text, global answers included."""
    answer = (Answer.objects
              .filter(Q(guild__isnull=True) | Q(guild=guild))
              .filter(text__iexact=text.strip()).first())
    if answer is None:
        raise ValueError(_("This answer is not in this server's library."))
    return answer


def register_variants(answer: Answer, texts: Iterable[str]) -> list[AnswerVariant]:
    """Register accepted variants of an answer, ignoring the useless ones."""
    variants = []
    for text in texts:
        text = str(text).strip()
        if not text or matching.normalize(text) == matching.normalize(answer.text):
            continue
        variant, _created = AnswerVariant.objects.get_or_create(
            answer=answer, text__iexact=text, defaults={'text': text})
        variants.append(variant)
    if variants:
        # The cached forms would still refuse the answer just registered.
        caching.forget_answer_forms(answer)
    return variants


def add_variant(guild: Guild, host_member: DiscordMember, answer_text: str,
                variant_text: str) -> AnswerVariant:
    """Register an accepted variant of a guild answer."""
    require_host(guild, host_member)
    answer = visible_answer(guild, answer_text)
    variant_text = variant_text.strip()
    if not variant_text:
        raise ValueError(_("Give the variant a text."))
    if matching.normalize(variant_text) == matching.normalize(answer.text):
        raise ValueError(_("This variant is already the answer itself."))
    if answer.variants.filter(text__iexact=variant_text).exists():
        raise ValueError(_("This variant is already registered."))
    variant = register_variants(answer, [variant_text])
    logger.info('Answer %s: variant %s registered', answer.pk, variant_text)
    return variant[0]


def remove_variant(guild: Guild, host_member: DiscordMember, answer_text: str,
                   variant_text: str) -> None:
    """Remove a variant registered for a guild answer."""
    require_host(guild, host_member)
    answer = visible_answer(guild, answer_text)
    removed, _counts = answer.variants.filter(
        text__iexact=variant_text.strip()).delete()
    if not removed:
        raise ValueError(_("This variant is not registered."))
    caching.forget_answer_forms(answer)
    logger.info('Answer %s: variant %s removed', answer.pk, variant_text)


def variants_of(guild: Guild, host_member: DiscordMember,
                answer_text: str) -> list[str]:
    """Return the variants accepted for a guild answer."""
    require_host(guild, host_member)
    answer = visible_answer(guild, answer_text)
    return [variant.text for variant in answer.variants.all()]


@transaction.atomic
def add_question(guild: Guild, host_member: DiscordMember, expected_text: str,
                 *, prompt: str = '', secondary_text: str = '',
                 year: int | None = None, album: str = '', media_url: str = '',
                 choices: Iterable[str] = (),
                 expected_variants: Iterable[str] = (),
                 secondary_variants: Iterable[str] = ()) -> dict:
    """Create a guild question and return its display label with its pk."""
    require_host(guild, host_member)
    year = clean_year(year)
    media_url = media_link(media_url)
    expected = add_answer(guild, host_member, expected_text)
    register_variants(expected, expected_variants)
    secondary = (add_answer(guild, host_member, secondary_text)
                 if secondary_text.strip() else None)
    if secondary is not None:
        register_variants(secondary, secondary_variants)
    question = Question.objects.create(
        guild=guild, prompt=prompt.strip(), expected_answer=expected,
        secondary_answer=secondary, year=year, album=album.strip(),
        media_url=media_url, author=author_of(host_member))
    given = [text for text in choices if str(text).strip()]
    for text in given:
        question.choices.add(add_answer(guild, host_member, text))
    if given:
        # Choices make it a multiple choice question: it has to be playable.
        problem = question_problem(question, QuizType.MULTIPLE_CHOICE)
        if problem:
            raise ValueError(problem)
    logger.info('%s: question %s created', guild, question)
    return {'pk': question.pk, 'label': question_line(question),
            'choices': question.choices.count(),
            'variants': question.expected_answer.variants.count()}


def author_of(host_member: DiscordMember) -> Player:
    """Return the player row a question of this host is authored by."""
    return Player.objects.from_discord(host_member)


def editable_question(guild: Guild, host_member: DiscordMember,
                      pk: int | str) -> Question:
    """Return a question of the guild's own library, host-checked.

    A host changes their own questions; an administrator of the server changes
    every one, the authorless ones included.
    """
    require_host(guild, host_member)
    number = str(pk).strip()
    question = None
    if number.isdigit():
        question = (Question.objects
                    .select_related('expected_answer', 'secondary_answer')
                    .filter(guild=guild, pk=int(number)).first())
    if question is None:
        raise ValueError(_("This question is not in this server's library."))
    author = player_of(host_member)
    authored = author is not None and question.author_id == author.pk
    if not authored and not can_manage_guild(host_member):
        raise PermissionError(_("Only the author of this question can change "
                                "it. Ask an administrator of the server."))
    return question


def relinked_answer(guild: Guild, host_member: DiscordMember,
                    text: str) -> Answer:
    """Return the answer a renamed question points at, reused or created."""
    canonical, variants = split_answers(text)
    answer = add_answer(guild, host_member, canonical)
    register_variants(answer, variants)
    return answer


@transaction.atomic
def set_variants(guild: Guild, host_member: DiscordMember, answer_text: str,
                 variants: Iterable[str]) -> list[AnswerVariant]:
    """Replace the texts an answer accepts with the ones given."""
    require_host(guild, host_member)
    answer = visible_answer(guild, answer_text)
    answer.variants.all().delete()
    given = register_variants(answer, variants)
    logger.info('%s: %s now accepts %s', guild, answer.text, len(given))
    return given


def remove_question(guild: Guild, host_member: DiscordMember,
                    pk: int | str) -> None:
    """Remove a question of the guild's library that no game has played."""
    question = editable_question(guild, host_member, pk)
    if question.rounds.exists():
        raise ValueError(_("This question was played in a game and cannot be "
                           "removed."))
    pk = question.pk
    question.delete()
    logger.info('%s: question %s removed', guild, pk)


def remove_answer(guild: Guild, host_member: DiscordMember,
                  pk: int | str) -> Answer:
    """Remove an answer of the guild's library that no question uses."""
    require_host(guild, host_member)
    number = str(pk).strip()
    answer = (guild.answers.filter(pk=int(number)).first()
              if number.isdigit() else None)
    if answer is None:
        raise ValueError(_("This answer is not in this server's library."))
    used_by = (answer.expected_for_questions.first()
               or answer.secondary_for_questions.first()
               or answer.choice_for_questions.first())
    if used_by is not None:
        raise ValueError(_('This answer is used by the question "%s" and '
                           'cannot be removed.') % question_line(used_by))
    answer.delete()
    logger.info('%s: answer %s removed', guild, answer.text)
    return answer


def require_rounds_playable(question: Question) -> None:
    """Raise ValueError when a queued round can no longer play the question."""
    rounds = (question.rounds.filter(started_at__isnull=True)
              .select_related('game'))
    for round_ in rounds:
        problem = question_problem(question, round_.effective_type)
        if problem:
            raise ValueError(_("A queued round already plays this question: "
                               "%s") % problem)


def _given_fields(**values: str) -> dict[str, str]:
    """Return the fields a host filled in, in question add order."""
    return {field: values[field] for field in EDITABLE_FIELDS
            if values.get(field) is not None}


def edit_question(guild: Guild, host_member: DiscordMember,
                  pk: int | str, *, answer: str | None = None,
                  artist: str | None = None, prompt: str | None = None,
                  choices: str | None = None, year: str | None = None,
                  album: str | None = None, media: str | None = None) -> dict:
    """Change the fields given and return the new display label.
    A field left out keeps its value, a field given as an empty string is cleared.
    Renaming an answer points the question at an existing or new answer of the guild
    and leaves the old one to the questions and rounds still using it.
    """
    given = _given_fields(answer=answer, artist=artist, prompt=prompt,
                          choices=choices, year=year, album=album, media=media)
    if not given:
        raise ValueError(_("Nothing to change."))
    question = editable_question(guild, host_member, pk)
    _edit_question(guild, host_member, question, **given)
    return {'pk': question.pk, 'fields': list(given),
            'label': question_line(question, with_answer=True),
            'media_url': question.media_url,
            'choices': question.choices.count()}


@transaction.atomic
def _edit_question(guild: Guild, host_member: DiscordMember, question: Question,
                   **given: str) -> Question:
    """Set the fields of a question a host has already been cleared for."""
    columns = []
    for field in EDITABLE_FIELDS:
        if field in given:
            column = _apply_field(guild, host_member, question, field,
                                  given[field])
            if column:
                columns.append(column)
    if columns:
        question.save(update_fields=columns)
    if 'choices' in given and question.choices.exists():
        # Choices make it a multiple choice question: it has to be playable.
        require_question_fits(question, QuizType.MULTIPLE_CHOICE)
    require_rounds_playable(question)
    logger.info('Question %s: %s changed', question.pk, ', '.join(given))
    return question


def _apply_field(guild: Guild, host_member: DiscordMember, question: Question,
                 field: str, value: str) -> str | None:
    """Apply one field of a question in memory; return its column, if any."""
    text = value.strip()
    drop = not text
    if field == 'answer':
        if drop:
            raise ValueError(_("A question needs an answer."))
        previous = question.expected_answer
        question.expected_answer = relinked_answer(guild, host_member, text)
        if question.choices.filter(pk=previous.pk).exists():
            # The renamed answer stays where it was among the options.
            question.choices.remove(previous)
            question.choices.add(question.expected_answer)
    elif field == 'artist':
        question.secondary_answer = (None if drop else
                                     relinked_answer(guild, host_member, text))
    elif field == 'choices':
        given = [] if drop else [part for part in value.split(',')
                                 if part.strip()]
        if not given and not drop:
            raise ValueError(_("Give the choices a text."))
        question.choices.set([add_answer(guild, host_member, part)
                              for part in given])
    elif field == 'prompt':
        question.prompt = '' if drop else text
    elif field == 'album':
        question.album = '' if drop else text
    elif field == 'media':
        question.media_url = '' if drop else media_link(text)
    elif field == 'year':
        question.year = None if drop else clean_year(text)
    # The choices set is a relation, it has no column of its own.
    return {'answer': 'expected_answer', 'artist': 'secondary_answer',
            'prompt': 'prompt', 'choices': None, 'year': 'year',
            'album': 'album', 'media': 'media_url'}[field]


def question_option(question: Question, guild: Guild | None = None) -> dict:
    """Return how a host picks a question: its pk, its label and its media.
    ``guild`` marks the questions of that guild's own library, as opposed to the
    ones of the global library every guild plays. ``author`` is the pk the
    spoiler rule is read with, so one cached list serves every viewer.
    """
    return {'pk': question.pk, 'label': question_line(question, with_answer=True),
            'media': bool(question.media_url),
            'author': question.author_id,
            'own': guild is not None and question.guild_id == guild.pk}


def require_question_fits(question: Question, quiz_type: str) -> None:
    """Raise ValueError when a quiz type cannot play the question."""
    problem = question_problem(question, quiz_type)
    if problem:
        raise ValueError(problem)


def viewer_questions(viewer: Player | None) -> Q:
    """Return the questions a viewer may read in the host panels.

    Only what they authored, unless they asked to see every question of the
    server; a question nobody authored is a spoiler like any other, and a
    caller that names no viewer reads none of them.
    """
    if viewer is None:
        return Q(pk__in=[])
    if viewer.show_all_questions:
        return Q()
    return Q(author=viewer)


def _all_questions(guild: Guild):
    """Return every question a guild plays, its own and the global ones."""
    return (Question.objects
            .select_related('expected_answer', 'secondary_answer')
            .filter(Q(guild__isnull=True) | Q(guild=guild))
            .order_by('-created_at'))


def _visible_questions(guild: Guild, viewer: Player | None):
    """Return the questions of the libraries a guild plays that one may read."""
    return _all_questions(guild).filter(viewer_questions(viewer))


def _render_library_options(guild: Guild) -> list[dict] | bool:
    """Return the options of the libraries a guild plays, False when too many.

    Every question is rendered, with its author; each viewer only reads their
    own slice of the one cached list.
    """
    rows = list(_all_questions(guild)[:caching.LIBRARY_CACHE_LIMIT + 1])
    if len(rows) > caching.LIBRARY_CACHE_LIMIT:
        return False
    return [question_option(question, guild) for question in rows]


def _library_options(guild: Guild) -> list[dict] | None:
    """Return the cached options of a guild's libraries, None when too many."""
    options = remember(caching.library_options_key(guild),
                       lambda: _render_library_options(guild), LIST_TIMEOUT)
    return None if options is False else options


def _used_question_pks(game: Game) -> tuple[int, ...]:
    """Return the questions the game already queued or played."""
    return remember(caching.used_questions_key(game),
                    lambda: tuple(game.rounds.values_list('question', flat=True)),
                    LIST_TIMEOUT)


def _shown_option(option: dict, viewer: Player | None) -> bool:
    """Return whether a rendered option is one the viewer may read."""
    if viewer is None:
        return False
    if viewer.show_all_questions:
        return True
    return option['author'] == viewer.pk


def question_choices(game: Game, viewer: Player | None, text: str = '',
                     limit: int = MAX_CHOICES) -> list[dict]:
    """Return the pk and label of questions the game did not use yet."""
    options = _library_options(game.guild)
    if options is None:
        return _query_question_choices(game, viewer, text, limit)
    used = _used_question_pks(game)
    wanted = text.strip().casefold()
    return [option for option in options
            if option['pk'] not in used
            and _shown_option(option, viewer)
            and (not wanted or wanted in option['label'].casefold())][:limit]


def queueable_questions(game: Game, viewer: Player | None):
    """Return the questions a game has not queued or played yet."""
    return _visible_questions(game.guild, viewer).exclude(
        pk__in=game.rounds.values('question'))


def _query_question_choices(game: Game, viewer: Player | None, text: str,
                            limit: int) -> list[dict]:
    """Return the unplayed options of a game straight from the database."""
    queryset = queueable_questions(game, viewer)
    text = text.strip()
    if text:
        queryset = queryset.filter(
            Q(prompt__icontains=text)
            | Q(expected_answer__text__icontains=text)
            | Q(secondary_answer__text__icontains=text))
    return [question_option(question, game.guild) for question in queryset[:limit]]


def question_by_pk(pk: int) -> Question:
    """Return a question by pk, with the answers of its label prefetched."""
    return (Question.objects
            .select_related('expected_answer', 'secondary_answer')
            .get(pk=pk))


def library_choices(guild: Guild, viewer: Player | None, text: str = '',
                    limit: int = MAX_CHOICES) -> list[dict]:
    """Return the questions of a guild's own library, as picker options."""
    options = _library_options(guild)
    if options is None:
        return _query_library_choices(guild, viewer, text, limit)
    wanted = text.strip().casefold()
    return [option for option in options
            if option['own']
            and _shown_option(option, viewer)
            and (not wanted or wanted in option['label'].casefold())][:limit]


def _query_library_choices(guild: Guild, viewer: Player | None, text: str,
                           limit: int) -> list[dict]:
    """Return the options of a guild's own library straight from the database."""
    queryset = _visible_questions(guild, viewer).filter(guild=guild)
    text = text.strip()
    if text:
        queryset = queryset.filter(
            Q(prompt__icontains=text)
            | Q(expected_answer__text__icontains=text)
            | Q(secondary_answer__text__icontains=text))
    return [question_option(question, guild) for question in queryset[:limit]]


def question_row(question: Question) -> dict:
    """Return one question of a library, as its library page shows it."""
    return {'pk': question.pk,
            'label': question_line(question, with_answer=True),
            'prompt': question.prompt,
            'answer': question.expected_answer.text,
            'artist': (question.secondary_answer.text
                       if question.secondary_answer else ''),
            'author': (question.author.discord_name or question.author.username
                       if question.author else ''),
            'year': question.year, 'album': question.album,
            'media_url': question.media_url,
            'variants': [variant.text
                         for variant in question.expected_answer.variants.all()],
            'choices': [choice.text for choice in question.choices.all()]}


def own_questions(guild: Guild, viewer: Player | None, text: str = '',
                  limit: int = LIBRARY_PAGE_SIZE) -> list[dict]:
    """Return the questions of a guild's own library, to fill and read."""
    queryset = (Question.objects
                .filter(guild=guild)
                .filter(viewer_questions(viewer))
                .select_related('expected_answer', 'secondary_answer', 'author')
                .prefetch_related('choices', 'expected_answer__variants')
                .order_by('expected_answer__text', 'pk'))
    wanted = text.strip()
    if wanted:
        queryset = queryset.filter(Q(prompt__icontains=wanted)
                                   | Q(expected_answer__text__icontains=wanted)
                                   | Q(secondary_answer__text__icontains=wanted))
    return [question_row(question) for question in queryset[:limit]]


def unused_questions(guild: Guild, viewer: Player | None,
                     limit: int = LIBRARY_PAGE_SIZE) -> list[dict]:
    """Return the questions of a guild's library that no game has played."""
    queryset = (Question.objects
                .filter(guild=guild, rounds__isnull=True)
                .filter(viewer_questions(viewer))
                .select_related('expected_answer', 'secondary_answer', 'author')
                .prefetch_related('expected_answer__variants')
                .order_by('expected_answer__text', 'pk')
                .distinct())
    return [question_row(question) for question in queryset[:limit]]


def set_show_all_questions(viewer: Player, show: bool) -> Player:
    """Read every question of a server as the viewer, or only their own."""
    viewer.show_all_questions = bool(show)
    viewer.save(update_fields=['show_all_questions'])
    return viewer


def unused_answers(guild: Guild,
                   limit: int = LIBRARY_PAGE_SIZE) -> list[dict]:
    """Return the answers of a guild's library that no question uses."""
    queryset = (Answer.objects
                .filter(guild=guild, expected_for_questions__isnull=True,
                        secondary_for_questions__isnull=True,
                        choice_for_questions__isnull=True)
                .prefetch_related('variants')
                .order_by('text')
                .distinct())
    return [{'pk': answer.pk, 'text': answer.text,
             'variants': [variant.text for variant in answer.variants.all()]}
            for answer in queryset[:limit]]
