"""Setting up a game, its queue, and what the control room shows."""


from collections.abc import Iterable
import logging

from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext as _ 
from discordcore.cache import LIST_TIMEOUT, STATE_TIMEOUT, remember
from discordcore.members import DiscordMember
from discordcore.mentions import user_mention
from discordcore.models import Guild, Player

from .. import caching
from .. import scoring
from ..constants import LIBRARY_PAGE_SIZE, MAX_CHOICES, QUIZ_OVER
from ..models import (Game, Question, QuizType, Round, ScoringMode,
                      question_problem)
from .guessing import answer_form, guess_of, round_display
from .guilds import require_host, target_channel_id, target_ping_role_id
from .library import question_choices, question_line
from .rounds import (create_round, current_round, queued_count,
                     require_visible, reveal_round, round_answers)
from .scores import _scored_rounds, game_scores, game_team_scores

logger = logging.getLogger(__name__)


def active_game(guild: Guild) -> Game | None:
    """Return the unplayed game of a guild, if any.
    Read uncached: ``create_game`` asks this to refuse a second live game, and a
    stale answer would let the unique constraint raise instead.
    """
    return (Game.objects.filter(guild=guild)
            .exclude(state=Game.State.FINISHED).first())


def played_game(guild: Guild) -> Game | None:
    """Return the game a guild is playing, for showing it; None when none is."""
    return remember(caching.active_game_key(guild),
                    lambda: active_game(guild), STATE_TIMEOUT)


def game_by_pk(guild: Guild, game_pk: int) -> Game:
    """Return the game of a guild, refusing one belonging to another server."""
    game = (Game.objects.filter(guild=guild, pk=game_pk)
            .select_related('host').first())
    if game is None:
        raise ValueError(_("This game is not one of this server."))
    return game


def game_rows(guild: Guild, limit: int = LIBRARY_PAGE_SIZE) -> list[dict]:
    """Return the games of a guild as the control room lists them."""
    return [{'pk': game.pk, 'game_name': game.display_name,
             'state': game.state, 'state_label': game.state_label,
             'questions': game.questions, 'created_at': game.created_at,
             'finished_at': game.finished_at, 'channel_id': game.channel_id}
            for game in _annotated_games(guild)[:limit]]


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
        raise ValueError(_("A game is already running in this server."))
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
    """Publish a game and return what its publication shows.
    A game being prepared becomes the one the server is playing.
    """
    require_host(game.guild, host_member)
    if not game.is_preparing:
        raise ValueError(_("This game is already published."))
    game.state = Game.State.RUNNING
    game.save(update_fields=['state'])
    logger.info('Game %s published', game.pk)
    return publication_payload(game)


def publication_payload(game: Game) -> dict:
    """Return what the publication of a published game shows."""
    return {**game_summary(game), 'game_id': game.pk,
            'scoring_label': ScoringMode(game.scoring_mode).display_name,
            'queued': queued_count(game), 'created_at': game.created_at,
            'channel_id': game.channel_id,
            'host_mention': user_mention(game.host.discord_user_id)}


def recap_payload(game: Game, rounds: Iterable[Round] | None = None) -> dict:
    """Return what the final scores of a finished game show."""
    rounds = _scored_rounds(game, rounds)
    return {'game_id': game.pk, 'game_name': game.display_name,
            'type_label': QuizType(game.type).display_name,
            'scoring_label': ScoringMode(game.scoring_mode).display_name,
            'scores': game_scores(game, rounds),
            'teams': game_team_scores(game, rounds),
            'rounds': len(rounds),
            'answers': sum(len(round_.guesses.all()) for round_ in rounds),
            'finished_at': game.finished_at,
            'channel_id': game.channel_id}


def end_game(game: Game, host_member: DiscordMember) -> dict:
    """End a game, publishing the round still open and its final scores.
    The round in progress is revealed as the reveal command does, and its display
    values come back under ``reveal`` for the caller to publish; ``reveal`` is
    None when no round was open.
    """
    require_host(game.guild, host_member)
    if game.state == Game.State.FINISHED:
        raise ValueError(_("This game is already over."))
    current = current_round(game)
    reveal = None
    if current is not None and not current.is_revealed:
        reveal = reveal_round(current, host_member)
    game.state = Game.State.FINISHED
    game.finished_at = timezone.now()
    game.save(update_fields=['state', 'finished_at'])
    logger.info('Game %s finished', game.pk)
    return {**recap_payload(game, _scored_rounds(game)), 'reveal': reveal}


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
    """Remove queued questions of a game and return how many were removed."""
    require_host(game.guild, host_member)
    removed, _counts = game.rounds.filter(
        pk__in=[int(pk) for pk in round_pks], started_at__isnull=True).delete()
    logger.info('Game %s: %s queued question(s) removed', game.pk, removed)
    return removed


def clear_queue(game: Game, host_member: DiscordMember) -> int:
    """Remove every queued question of a game, and return how many."""
    require_host(game.guild, host_member)
    removed, _counts = game.rounds.filter(started_at__isnull=True).delete()
    logger.info('Game %s: queue cleared, %s removed', game.pk, removed)
    return removed


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


def copyable_games(guild: Guild, current: Game | None = None):
    """Return the games of a guild a host may copy questions from."""
    games = _annotated_games(guild)
    return games.exclude(pk=current.pk) if current is not None else games


def _query_game_choices(guild: Guild, current: Game | None, text: str,
                         limit: int) -> list[dict]:
    """Return a guild's game options straight from the database."""
    games = copyable_games(guild, current)
    if text.strip():
        games = games.filter(name__icontains=text.strip())
    return [{'pk': game.pk, 'label': game_option(game, game.questions)}
            for game in games[:limit]]


def control_state(game: Game, player: Player | None = None) -> dict:
    """Return everything the control room of a game shows at one moment.
    The round in play with its display values and its answers, the queue, and
    the standings. Built from the same reads the embeds are built from, so what
    a host sees in the browser and what the server was told cannot disagree.
    The rounds are read once and scored from that read: the control room of a
    live game is polled every couple of seconds.
    """
    round_ = current_round(game)
    rounds = _scored_rounds(game)
    state = {**game_summary(game), 'game_pk': game.pk, 'state': game.state,
             'state_label': game.state_label, 'queued': queued_count(game),
             'rounds': len(rounds), 'has_teams': game.teams.exists(),
             'can_publish': game.is_preparing,
             'can_round': game.is_running,
             'can_next': (game.is_running
                          and (round_ is None or round_.is_revealed)),
             'can_reveal': round_ is not None and round_.is_active,
             'can_queue': game.is_active,
             'can_end': game.state != Game.State.FINISHED}
    standings = {'scores': game_scores(game, rounds),
                 'teams': game_team_scores(game, rounds)
                 if state['has_teams'] else []}
    if round_ is None:
        return {**state, 'round': None, **standings}
    display = round_display(round_)
    guess = None if player is None else guess_of(round_, player)
    return {**state, **standings,
            'round': {**display, 'is_active': round_.is_active,
                      'answers': round_answers(round_),
                      'form': answer_form(display),
                      'guessed': guess is not None,
                      'guessed_text': guess.text if guess is not None else '',
                      'guessed_secondary': (guess.secondary_text
                                            if guess is not None else ''),
                      'guessed_correct': (bool(guess.text_correct)
                                          if guess is not None else False),
                      'guessed_secondary_correct': (
                          bool(guess.secondary_correct)
                          if guess is not None else False)}}
