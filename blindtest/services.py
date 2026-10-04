"""Quiz game logic, kept synchronous so every caller shares it.

The Discord bot calls these helpers through ``discordbot.db.run_db``; the admin,
tests and the future web front end call them directly.
"""

import logging
import random
from collections.abc import Iterable
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Count, F, Max, Prefetch, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from discordcore.cache import (LIST_TIMEOUT, STATE_TIMEOUT, forget, guild_row_key,
                             remember)
from discordcore.members import (DiscordGuild, DiscordMember, can_manage_guild,
                                 member_mentions)
from discordcore.mentions import normalize_mention
from discordcore.models import Guild, Host, Player

from . import caching, matching, scoring
from .constants import (CLEAR_VALUE, EDITABLE_FIELDS, MAX_CHOICES,
                        MAX_LISTED_PLAYERS, MAX_YEAR, QUIZ_OVER)
from .models import (Answer, AnswerVariant, Game, Guess, Question, QuizType, Round,
                     ScoringMode, Team, question_problem)

logger = logging.getLogger(__name__)


def is_host(guild: Guild, host_member: DiscordMember) -> bool:
    """Return True when the member may host games in the guild.

    Hosts are the guild's host mentions (a Discord user or a role), plus the
    members Discord allows to manage the server.
    """
    hosts = set(hosts_of(guild))
    return bool(hosts & member_mentions(host_member)) or can_manage_guild(host_member)


def require_host(guild: Guild, host_member: DiscordMember) -> None:
    """Raise PermissionError when the member is not a host of the guild."""
    if not is_host(guild, host_member):
        raise PermissionError(_("Only hosts of this server can run a quiz. "
                                "Ask an administrator for the host permission."))


def require_admin(member: DiscordMember) -> None:
    """Raise PermissionError unless the member may manage the Discord server."""
    if not can_manage_guild(member):
        raise PermissionError(_("Only server administrators can manage the "
                                "settings of a server."))


def add_host(guild: Guild, mention: str,
             admin_member: DiscordMember) -> Host:
    """Allow a Discord user or role to host games in the guild."""
    require_admin(admin_member)
    host, created = Host.objects.get_or_create(guild=guild,
                                               mention=normalize_mention(mention))
    if created:
        logger.info('%s is now a host of %s', host.mention, guild)
    return host


def remove_host(guild: Guild, mention: str,
                admin_member: DiscordMember) -> Guild:
    """Withdraw the host rights of a Discord user or role in the guild."""
    require_admin(admin_member)
    matches = guild.hosts.filter(mention=normalize_mention(mention))
    if not matches.exists():
        raise ValueError(_("This user or role is not a host of this server."))
    matches.delete()
    return guild


def hosts_of(guild: Guild) -> list[str]:
    """Return the mentions allowed to host games in the guild."""
    return remember(caching.hosts_key(guild),
                    lambda: list(guild.hosts.values_list('mention', flat=True)),
                    STATE_TIMEOUT)


def default_channel_of(guild: Guild) -> int | None:
    """Return the channel games of the guild default to, or None."""
    return guild.default_channel_id


def set_default_channel(guild: Guild, channel_id: int,
                        admin_member: DiscordMember) -> Guild:
    """Make ``channel_id`` the channel the guild's games are played in."""
    require_admin(admin_member)
    guild.default_channel_id = channel_id
    guild.save(update_fields=['default_channel_id'])
    # The cached row would hand the next command the previous default.
    forget(guild_row_key(guild.discord_id))
    logger.info('%s: %s is now the default channel', guild, channel_id)
    return guild


def clear_default_channel(guild: Guild, admin_member: DiscordMember) -> Guild:
    """Send the guild's games back to the channel their host used."""
    require_admin(admin_member)
    guild.default_channel_id = None
    guild.save(update_fields=['default_channel_id'])
    forget(guild_row_key(guild.discord_id))
    logger.info('%s: default channel cleared', guild)
    return guild


def default_ping_role_of(guild: Guild) -> int | None:
    """Return the role pinged for the guild's games."""
    return guild.default_ping_role_id


def set_default_ping_role(guild: Guild, role_id: int,
                          admin_member: DiscordMember) -> Guild:
    """Make ``role_id`` the role the guild's games will ping."""
    require_admin(admin_member)
    guild.default_ping_role_id = role_id
    guild.save(update_fields=['default_ping_role_id'])
    # The cached row would hand the next command the previous default.
    forget(guild_row_key(guild.discord_id))
    logger.info('%s: %s is now the default ping role', guild, role_id)
    return guild


def clear_default_ping_role(guild: Guild, admin_member: DiscordMember) -> Guild:
    """Clear the default guild's ping."""
    require_admin(admin_member)
    guild.default_ping_role_id = None
    guild.save(update_fields=['default_ping_role_id'])
    forget(guild_row_key(guild.discord_id))
    logger.info('%s: default ping role cleared', guild)
    return guild


def target_ping_role_id(guild: Guild, ping_role_id: int | None) -> int | None:
    """Return the role a game pings: the one set, else the guild default.

    No ping if neither is known.
    """
    return ping_role_id if ping_role_id is not None else guild.default_ping_role_id


def target_channel_id(guild: Guild, channel_id: int | None,
                      invoking_id: int | None = None) -> int:
    """Return the channel a game is played in, in the following order of preference.

    The channel the host named, then the channel the server defaults to,
    then the channel the command was run in.
    """
    for candidate in (channel_id, guild.default_channel_id, invoking_id):
        if candidate is not None:
            return candidate
    raise ValueError(_("Give a channel to play this game in."))


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
    """Drop a variant registered for a guild answer."""
    require_host(guild, host_member)
    answer = visible_answer(guild, answer_text)
    removed, _counts = answer.variants.filter(
        text__iexact=variant_text.strip()).delete()
    if not removed:
        raise ValueError(_("This variant is not registered."))
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
        media_url=media_url)
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


def editable_question(guild: Guild, host_member: DiscordMember,
                      pk: int | str) -> Question:
    """Return a question of the guild's own library, host-checked."""
    require_host(guild, host_member)
    number = str(pk).strip()
    question = None
    if number.isdigit():
        question = (Question.objects
                    .select_related('expected_answer', 'secondary_answer')
                    .filter(guild=guild, pk=int(number)).first())
    if question is None:
        raise ValueError(_("This question is not in this server's library."))
    return question


def relinked_answer(guild: Guild, host_member: DiscordMember,
                    text: str) -> Answer:
    """Return the answer a renamed question points at, reused or created."""
    canonical, variants = split_answers(text)
    answer = add_answer(guild, host_member, canonical)
    register_variants(answer, variants)
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
    return {field: values.get(field, '') for field in EDITABLE_FIELDS
            if values.get(field, '').strip()}


def edit_question(guild: Guild, host_member: DiscordMember,
                  pk: int | str, *, answer: str = '', artist: str = '',
                  prompt: str = '', choices: str = '', year: str = '',
                  album: str = '', media: str = '') -> dict:
    """Change the fields a host filled in and return the new display label.

    An empty field keeps its value, ``-`` drops it. Renaming an answer points the
    question at an existing or new answer of the guild and leaves the old one to
    the questions and rounds still using it.
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
                   *, answer: str = '', artist: str = '', prompt: str = '',
                   choices: str = '', year: str = '', album: str = '',
                   media: str = '') -> Question:
    """Set the fields of a question a host has already been cleared for."""
    given = _given_fields(answer=answer, artist=artist, prompt=prompt,
                          choices=choices, year=year, album=album, media=media)
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
    drop = text == CLEAR_VALUE
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


def guild_by_discord_id(discord_id: int) -> Guild | None:
    """Return the guild row of a Discord server, or None when it is unknown."""
    return Guild.objects.filter(discord_id=discord_id).first()


def guilds_by_discord_ids(discord_ids: Iterable[int]) -> dict[int, Guild]:
    """Return the guild rows of the Discord IDs given, keyed by Discord ID."""
    return {guild.discord_id: guild
            for guild in Guild.objects.filter(discord_id__in=list(discord_ids))}


def add_guild(discord_guild: DiscordGuild, admin_member: DiscordMember) -> Guild:
    """Register a Discord server the web admin was asked to manage."""
    require_admin(admin_member)
    return Guild.objects.from_discord(discord_guild)


def games_count(guild: Guild) -> int:
    """Return the number of games ever started in the guild."""
    return Game.objects.filter(guild=guild).count()


def active_game(guild: Guild) -> Game | None:
    """Return the unplayed game of a guild, if any."""
    return (Game.objects.filter(guild=guild)
            .exclude(state=Game.State.FINISHED).first())


def create_game(guild: Guild, channel_id: int | None, host_member: DiscordMember,
                scoring_mode: str = ScoringMode.STANDARD, *,
                name: str = '', quiz_type: str = QuizType.BLIND_TEST,
                state: str = Game.State.RUNNING,
                invoking_id: int | None = None,
                ping_role_id: int | None = None) -> Game:
    """Create a game in a channel of ``guild``.

    The player row of the host is created when missing and recorded on the game.
    The game is played in the channel the host named, then in the guild's
    default channel, then in the one the host started it from. It pings the
    role the host named, else the guild's default one.
    """
    require_host(guild, host_member)
    if scoring_mode not in ScoringMode.values:
        raise ValueError(_("Unknown scoring mode."))
    if quiz_type not in QuizType.values:
        raise ValueError(_("Unknown quiz type."))
    target_id = target_channel_id(guild, channel_id, invoking_id)
    active = active_game(guild)
    if active is not None:
        if active.is_preparing:
            raise ValueError(_("A game is being prepared: %(name)s. Publish it "
                               "with /quiz publish, or end it first.")
                             % {'name': active.display_name})
        raise ValueError(_("A quiz is already running in this server."))
    game = Game.objects.create(guild=guild, channel_id=target_id,
                               ping_role_id=target_ping_role_id(
                                   guild, ping_role_id),
                               host=Player.objects.from_discord(host_member),
                               scoring_mode=scoring_mode, name=name.strip(),
                               type=quiz_type, state=state)
    logger.info('Game %s created by %s in %s', game.pk, game.host, guild)
    return game


def game_summary(game: Game) -> dict:
    """Return the display values naming a game and how it was set up.

    Both panels start from these, so a host reads the same line whichever
    controls they are holding.
    """
    return {'game_name': game.display_name,
            'type_label': QuizType(game.type).display_name,
            'channel_id': game.channel_id,
            'ping_role_id': game.ping_role_id}


def panel_data(game: Game) -> dict:
    """Return the display values the setup panel of a game shows."""
    return {**game_summary(game), 'game_id': game.pk,
            'queued': queued_count(game),
            'choices': question_choices(game),
            'queued_choices': queued_choices(game),
            'games': game_choices(game.guild, game)}


def publish_game(game: Game, host_member: DiscordMember) -> dict:
    """Publish a game and return what its announcement shows.

    A game being prepared becomes the one the server is playing.
    """
    require_host(game.guild, host_member)
    if not game.is_preparing:
        raise ValueError(_("This game is already published."))
    game.state = Game.State.RUNNING
    game.save(update_fields=['state'])
    logger.info('Game %s published', game.pk)
    return {**game_summary(game), 'game_id': game.pk,
            'scoring_label': ScoringMode(game.scoring_mode).display_name,
            'queued': queued_count(game), 'created_at': game.created_at,
            'channel_id': game.channel_id}


def finish_game(game: Game, host_member: DiscordMember) -> dict:
    """End a game, publishing the round still open and its final scores.

    The round in progress is revealed as the reveal command does, and its display
    values come back under ``reveal`` for the caller to publish; ``reveal`` is
    None when no round was open.
    """
    require_host(game.guild, host_member)
    if game.state == Game.State.FINISHED:
        raise ValueError(_("This quiz is already over."))
    current = current_round(game)
    reveal = None
    if current is not None and not current.is_revealed:
        reveal = reveal_round(current, host_member)
    game.state = Game.State.FINISHED
    game.finished_at = timezone.now()
    game.save(update_fields=['state', 'finished_at'])
    logger.info('Game %s finished', game.pk)
    rounds = _scored_rounds(game)
    return {'game_id': game.pk, 'game_name': game.display_name,
            'type_label': QuizType(game.type).display_name,
            'scoring_label': ScoringMode(game.scoring_mode).display_name,
            'scores': game_scores(game, rounds),
            'teams': game_team_scores(game, rounds),
            'rounds': len(rounds),
            'answers': sum(len(round_.guesses.all()) for round_ in rounds),
            'finished_at': game.finished_at,
            'reveal': reveal,
            'channel_id': game.channel_id}


def current_round(game: Game) -> Round | None:
    """Return the round currently in play in the game, if any."""
    return game.rounds.filter(started_at__isnull=False).order_by('-index').first()


def next_index(game: Game) -> int:
    """Return the round number to give the next round of the game."""
    total = game.rounds.aggregate(total=Max('index'))['total']
    return (total or 0) + 1


def require_visible(game: Game, question: Question) -> None:
    """Raise ValueError when the question belongs to another guild."""
    if question.guild_id is not None and question.guild_id != game.guild_id:
        raise ValueError(_("This question belongs to another server."))


def queued_count(game: Game) -> int:
    """Return how many questions of the game are queued and not started."""
    return game.rounds.filter(started_at__isnull=True).count()


def question_option(question: Question, guild: Guild | None = None) -> dict:
    """Return how a host picks a question: its pk, its label and its media.

    ``guild`` marks the questions of that guild's own library, as opposed to the
    ones of the global library every guild plays.
    """
    return {'pk': question.pk, 'label': question_line(question, with_answer=True),
            'media': bool(question.media_url),
            'own': guild is not None and question.guild_id == guild.pk}


def queued_choices(game: Game, text: str = '',
                   limit: int = MAX_CHOICES) -> list[dict]:
    """Return the questions queued for a game, as picker options."""
    choices = remember(caching.queued_options_key(game),
                       lambda: _render_queued_options(game), LIST_TIMEOUT)
    wanted = text.strip().casefold()
    return [choice for choice in choices
            if not wanted or wanted in choice['label'].casefold()][:limit]


def _render_queued_options(game: Game) -> list[dict]:
    """Return the options of the rounds a game queued, in round order."""
    rounds = (game.rounds.filter(started_at__isnull=True)
              .select_related('question__expected_answer',
                              'question__secondary_answer')
              .order_by('index')[:caching.QUEUE_CACHE_LIMIT])
    return [{'pk': round_.pk,
             'label': f'#{round_.index} '
                      f'{question_line(round_.question, with_answer=True)}',
             'media': bool(round_.question.media_url)}
            for round_ in rounds]


def queue_questions(game: Game, host_member: DiscordMember, pks: Iterable[int],
                    quiz_type: str = '') -> dict:
    """Queue questions for a game, skipping the ones its type cannot play."""
    require_host(game.guild, host_member)
    if not game.is_active:
        raise ValueError(QUIZ_OVER)
    added = skipped = 0
    for pk in pks:
        question = Question.objects.filter(pk=pk).first()
        if question is None:
            skipped += 1
            continue
        require_visible(game, question)
        if question_problem(question, quiz_type or game.type):
            skipped += 1
            continue
        create_round(game, host_member, question, quiz_type)
        added += 1
    logger.info('Game %s: %s question(s) queued, %s skipped',
                game.pk, added, skipped)
    return {'added': added, 'skipped': skipped, 'queued': queued_count(game)}


def unqueue_questions(game: Game, host_member: DiscordMember,
                      round_pks: Iterable[int]) -> int:
    """Drop queued questions of a game and return how many were dropped."""
    require_host(game.guild, host_member)
    dropped, _counts = game.rounds.filter(
        pk__in=[int(pk) for pk in round_pks], started_at__isnull=True).delete()
    logger.info('Game %s: %s queued question(s) dropped', game.pk, dropped)
    return dropped


def clear_queue(game: Game, host_member: DiscordMember) -> int:
    """Drop every queued question of a game and return how many were dropped."""
    require_host(game.guild, host_member)
    dropped, _counts = game.rounds.filter(started_at__isnull=True).delete()
    logger.info('Game %s: queue cleared, %s dropped', game.pk, dropped)
    return dropped


def copy_questions(game: Game, host_member: DiscordMember, source: Game,
                   quiz_type: str = '') -> dict:
    """Queue the questions of another game, skipping the ones it cannot play."""
    require_host(game.guild, host_member)
    if source.guild_id != game.guild_id:
        raise ValueError(_("This game belongs to another server."))
    if source.pk == game.pk:
        raise ValueError(_("Pick another game to copy from."))
    pks = list(source.rounds.order_by('index').values_list('question', flat=True))
    return queue_questions(game, host_member, pks, quiz_type)


def copy_questions_by_pk(game: Game, host_member: DiscordMember, source_pk: int,
                         quiz_type: str = '') -> dict:
    """Queue the questions of another game, named by its pk."""
    source = Game.objects.filter(pk=int(source_pk)).first()
    if source is None:
        raise ValueError(_("This game no longer exists."))
    return copy_questions(game, host_member, source, quiz_type)


def game_option(game: Game, questions: int) -> str:
    """Return how a host picks a game to copy: its name, size and state."""
    return (f'{game.display_name} — {questions} question(s) — '
            f'{game.State(game.state).display_name}')


def game_choices(guild: Guild, current: Game | None = None, text: str = '',
                 limit: int = MAX_CHOICES) -> list[dict]:
    """Return the games of a guild a host may copy questions from."""
    options = remember(caching.game_options_key(guild),
                       lambda: _render_game_options(guild), LIST_TIMEOUT)
    if options is False:
        return _query_game_choices(guild, current, text, limit)
    wanted = text.strip().casefold()
    return [option for option in options
            if (current is None or option['pk'] != current.pk)
            and (not wanted or wanted in option['label'].casefold())][:limit]


def _annotated_games(guild: Guild):
    """Return the games of a guild, newest first, with their round count."""
    return (Game.objects.filter(guild=guild)
            .annotate(questions=Count('rounds')).order_by('-created_at'))


def _render_game_options(guild: Guild) -> list[dict] | bool:
    """Return the options of a guild's games, False when there are too many."""
    rows = list(_annotated_games(guild)[:caching.GAME_CACHE_LIMIT + 1])
    if len(rows) > caching.GAME_CACHE_LIMIT:
        return False
    return [{'pk': game.pk, 'label': game_option(game, game.questions)}
            for game in rows]


def _query_game_choices(guild: Guild, current: Game | None, text: str,
                        limit: int) -> list[dict]:
    """Return a guild's game options straight from the database."""
    games = _annotated_games(guild)
    if current is not None:
        games = games.exclude(pk=current.pk)
    if text.strip():
        games = games.filter(name__icontains=text.strip())
    return [{'pk': game.pk, 'label': game_option(game, game.questions)}
            for game in games[:limit]]


def require_question_fits(question: Question, quiz_type: str) -> None:
    """Raise ValueError when a quiz type cannot play the question."""
    problem = question_problem(question, quiz_type)
    if problem:
        raise ValueError(problem)


def type_override(quiz_type: str, game: Game) -> str:
    """Return the type to store on a round: its override, or empty to inherit."""
    return '' if quiz_type == game.type else quiz_type


def _queue_round(game: Game, host_member: DiscordMember, question: Question,
                 quiz_type: str = '', index: int | None = None) -> Round:
    """Queue a round ahead of time so the host does not pick it live."""
    require_host(game.guild, host_member)
    if not game.is_active:
        raise ValueError(QUIZ_OVER)
    require_visible(game, question)
    quiz_type = quiz_type or game.type
    require_question_fits(question, quiz_type)
    if index is None:
        index = next_index(game)
    round_ = Round.objects.create(game=game, index=index, question=question,
                                  type=type_override(quiz_type, game))
    logger.info('Game %s round %s queued on %s', game.pk, index, question)
    return round_


def create_round(game: Game, host_member: DiscordMember, question: Question,
                 quiz_type: str = '', index: int | None = None) -> dict:
    """Queue a round and return the display values the queue note shows."""
    return round_display(_queue_round(game, host_member, question, quiz_type,
                                      index))


def start_round(game: Game, host_member: DiscordMember,
                question: Question | None = None,
                quiz_type: str = '') -> dict:
    """Open the next round of the game, and return what its message shows.

    Queued rounds created beforehand are started in their index order;
    otherwise the given question (or an unplayed one) is drawn.
    """
    require_host(game.guild, host_member)
    if game.is_preparing:
        raise ValueError(_("Publish the game before opening a round."))
    if not game.is_running:
        raise ValueError(QUIZ_OVER)
    current = current_round(game)
    if current is not None and not current.is_revealed:
        raise ValueError(_("Reveal the current round before starting the next one."))
    queued = game.rounds.filter(started_at__isnull=True).order_by('index').first()
    if question is not None:
        require_visible(game, question)
        if queued is None:
            queued = Round.objects.create(game=game, index=next_index(game),
                                          question=question)
        round_type = quiz_type or queued.effective_type
        require_question_fits(question, round_type)
        queued.question = question
        queued.type = type_override(round_type, game)
        queued.started_at = timezone.now()
        queued.save(update_fields=['question', 'type', 'started_at'])
    else:
        if queued is None:
            round_type = quiz_type or game.type
            question = pick_question(game, round_type)
            queued = Round.objects.create(
                game=game, index=next_index(game), question=question,
                type=type_override(round_type, game))
        queued.started_at = timezone.now()
        queued.save(update_fields=['started_at'])
    logger.info('Game %s round %s started on %s', game.pk, queued.index,
                queued.question)
    display = round_display(queued)
    display['queued'] = queued_count(game)
    display['form'] = answer_form(display)
    display['ping_role_id'] = game.ping_role_id
    return display


def playable_questions(game: Game, quiz_type: str):
    """Return the questions of the game library the type can still play."""
    used = game.rounds.values('question')
    candidates = (Question.objects
                  .filter(Q(guild__isnull=True) | Q(guild=game.guild))
                  .exclude(pk__in=used))
    if quiz_type != QuizType.BLIND_TEST:
        candidates = candidates.exclude(prompt='')
    if quiz_type == QuizType.MULTIPLE_CHOICE:
        candidates = (candidates
                      .annotate(choice_count=Count('choices', distinct=True))
                      .filter(choice_count__gte=2, choices=F('expected_answer')))
    return candidates


def pick_question(game: Game, quiz_type: str = QuizType.BLIND_TEST) -> Question:
    """Return a random unplayed question the quiz type can play."""
    candidates = playable_questions(game, quiz_type)
    pks = list(candidates.values_list('pk', flat=True).distinct())
    if not pks:
        raise ValueError(_("No unplayed question left for this round type. "
                           "Add more questions in the admin."))
    return candidates.get(pk=random.choice(pks))


def _visible_questions(guild: Guild):
    """Return the questions a guild plays, its own and the global ones."""
    return (Question.objects
            .select_related('expected_answer', 'secondary_answer')
            .filter(Q(guild__isnull=True) | Q(guild=guild))
            .order_by('-created_at'))


def _render_library_options(guild: Guild) -> list[dict] | bool:
    """Return the options of the libraries a guild plays, False when too many."""
    rows = list(_visible_questions(guild)[:caching.LIBRARY_CACHE_LIMIT + 1])
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


def question_choices(game: Game, text: str = '',
                     limit: int = MAX_CHOICES) -> list[dict]:
    """Return the pk and label of questions the game did not use yet."""
    options = _library_options(game.guild)
    if options is None:
        return _query_question_choices(game, text, limit)
    used = _used_question_pks(game)
    wanted = text.strip().casefold()
    return [option for option in options
            if option['pk'] not in used
            and (not wanted or wanted in option['label'].casefold())][:limit]


def _query_question_choices(game: Game, text: str, limit: int) -> list[dict]:
    """Return the unplayed options of a game straight from the database."""
    queryset = _visible_questions(game.guild).exclude(
        pk__in=game.rounds.values('question'))
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


def library_choices(guild: Guild, text: str = '',
                    limit: int = MAX_CHOICES) -> list[dict]:
    """Return the questions of a guild's own library, as picker options."""
    options = _library_options(guild)
    if options is None:
        return _query_library_choices(guild, text, limit)
    wanted = text.strip().casefold()
    return [option for option in options
            if option['own']
            and (not wanted or wanted in option['label'].casefold())][:limit]


def _query_library_choices(guild: Guild, text: str, limit: int) -> list[dict]:
    """Return the options of a guild's own library straight from the database."""
    queryset = _visible_questions(guild).filter(guild=guild)
    text = text.strip()
    if text:
        queryset = queryset.filter(
            Q(prompt__icontains=text)
            | Q(expected_answer__text__icontains=text)
            | Q(secondary_answer__text__icontains=text))
    return [question_option(question, guild) for question in queryset[:limit]]


def round_display(round_: Round) -> dict:
    """Return the display values of a round, with its question prefetched."""
    round_ = (Round.objects
              .select_related('game', 'question__expected_answer',
                              'question__secondary_answer')
              .prefetch_related('question__choices')
              .get(pk=round_.pk))
    question = round_.question
    return {'round_id': round_.pk, 'index': round_.index,
            'prompt': question.effective_prompt,
            'prompt_set': bool(question.prompt.strip()),
            'media_url': question.media_url,
            'question_text': question_line(question),
            'host_text': question_line(question, with_answer=True),
            'answer_text': question.answer_text,
            'expected': question.expected_answer.text,
            'type': round_.effective_type,
            'type_label': QuizType(round_.effective_type).display_name,
            'scoring_label': ScoringMode(round_.effective_scoring_mode).display_name,
            'game_id': round_.game_id,
            'game_name': round_.game.display_name,
            'options': [{'pk': choice.pk, 'label': choice.text}
                        for choice in question.choices.all()][:MAX_CHOICES]}


def answer_form(display: dict) -> dict:
    """Return the answer form of a round: its type and the choices to offer."""
    multiple_choice = display['type'] == QuizType.MULTIPLE_CHOICE
    return {'round_id': display['round_id'], 'index': display['index'],
            'type': display['type'],
            'options': display['options'] if multiple_choice else []}


def guess_form(round_: Round) -> dict:
    """Return what the answer form of a round needs: its type and its options."""
    return answer_form(round_display(round_))


def round_answers(round_: Round) -> dict:
    """Return how many players answered a round and who was right."""
    guesses = list(round_.guesses.select_related('player'))
    right = [guess for guess in guesses
             if guess.text_correct and guess.player is not None]
    return {'answered': len(guesses), 'right': len(right),
            'right_names': [guess.player.discord_name or guess.player.username
                            for guess in right][:MAX_LISTED_PLAYERS]}


def reveal_round(round_: Round, host_member: DiscordMember) -> dict:
    """Reveal a round and return its answer with the standings."""
    round_ = _reveal_round(round_, host_member)
    display = round_display(round_)
    display['scores'] = game_scores(round_.game)
    display.update(round_answers(round_))
    return display


def _reveal_round(round_: Round, host_member: DiscordMember) -> Round:
    """Mark a round as revealed, which ends it for the players."""
    require_host(round_.game.guild, host_member)
    if round_.is_revealed:
        raise ValueError(_("This round is already revealed."))
    round_.revealed_at = timezone.now()
    round_.save(update_fields=['revealed_at'])
    return round_


def _team_for(round_: Round, player: Player) -> Team | None:
    """Return the team of a player in the game of a round, if any."""
    return Team.objects.filter(game_id=round_.game_id, players=player).first()


def submit_guess(round_: Round, player: Player, text: str = '',
                 secondary_text: str = '') -> Guess:
    """Record a player's first answer for a round; the points come at reveal."""
    if not round_.is_started:
        raise ValueError(_("This round has not started yet."))
    if round_.is_revealed:
        raise ValueError(_("This round is already revealed."))
    if Guess.objects.filter(round=round_, player=player).exists():
        raise ValueError(_("You already answered this round."))
    text = text.strip()
    secondary_text = secondary_text.strip()
    if not text and not secondary_text:
        raise ValueError(_("Give at least an answer."))
    question = (Question.objects
                .select_related('expected_answer', 'secondary_answer')
                .get(pk=round_.question_id))
    expected = question.expected_answer
    secondary = question.secondary_answer
    guess = Guess.objects.create(
        round=round_, player=player, team=_team_for(round_, player),
        text=text, secondary_text=secondary_text,
        text_correct=matching.matches_any(
            text, expected.text,
            [variant.text for variant in expected.variants.all()]),
        secondary_correct=(
            matching.matches_any(
                secondary_text, secondary.text,
                [variant.text for variant in secondary.variants.all()])
            if secondary else False))
    logger.info('Game %s round %s: %s answered', round_.game_id, round_.index,
                player)
    return guess


def submit_multiple_choice(round_: Round, player: Player, choice_pk: int,
                           secondary_text: str = '') -> Guess:
    """Record the choice a player picked in a multiple choice round."""
    choice = round_.question.choices.filter(pk=choice_pk).first()
    if choice is None:
        raise ValueError(_("This choice is not offered by this round."))
    return submit_guess(round_, player, choice.text, secondary_text)


def _scored_rounds(game: Game,
                   rounds: Iterable[Round] | None = None) -> list[Round]:
    """Return the rounds a game played, with their guesses, ready to score."""
    if rounds is None:
        rounds = game.rounds.filter(started_at__isnull=False).prefetch_related(
            Prefetch('guesses', queryset=Guess.objects
                     .select_related('player').order_by('submitted_at', 'pk')))
    return list(rounds)


def game_scores(game: Game,
                rounds: Iterable[Round] | None = None) -> list[dict]:
    """Return the total points per player of the game, best first.

    ``rounds`` spares a caller that read the rounds of the game already.
    """
    totals: dict[int, dict] = {}
    for round_ in _scored_rounds(game, rounds):
        guesses = list(round_.guesses.all())
        points = scoring.round_points(round_, guesses)
        for guess in guesses:
            if guess.player is None:
                continue
            row = totals.setdefault(guess.player_id, {
                'username': guess.player.username,
                'discord_name': guess.player.discord_name,
                'points': 0,
            })
            row['points'] += points.get(guess.pk, 0)
    return sorted(totals.values(), key=lambda row: (-row['points'],
                                                    row['username']))


def add_team(game: Game, host_member: DiscordMember, name: str,
             players: Iterable[Player] = ()) -> Team:
    """Create a team in a game, optionally with its first players."""
    require_host(game.guild, host_member)
    name = name.strip()
    if not name:
        raise ValueError(_("Give the team a name."))
    team = Team.objects.create(game=game, name=name)
    for player in players:
        assign_player(team, host_member, player)
    logger.info('Game %s: team %s created', game.pk, name)
    return team


def assign_player(team: Team, host_member: DiscordMember, player: Player) -> Team:
    """Put a player in a team, moving them out of their other team of the game."""
    require_host(team.game.guild, host_member)
    other = team_of(team.game, player)
    if other is not None and other.pk != team.pk:
        other.players.remove(player)
    team.players.add(player)
    return team


def team_of(game: Game, player: Player) -> Team | None:
    """Return the team of the player in this game, if any."""
    return player.teams.filter(game=game).first()


def game_team_scores(game: Game,
                     rounds: Iterable[Round] | None = None) -> list[dict]:
    """Return the total points per team of the game, best first."""
    totals = {team.pk: {'name': team.name, 'points': 0}
              for team in game.teams.all()}
    for round_ in _scored_rounds(game, rounds):
        guesses = list(round_.guesses.all())
        points = scoring.round_points(round_, guesses)
        for guess in guesses:
            if guess.team_id in totals:
                totals[guess.team_id]['points'] += points.get(guess.pk, 0)
    return sorted(totals.values(), key=lambda row: (-row['points'], row['name']))
