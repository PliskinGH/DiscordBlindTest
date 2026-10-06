"""Opening and revealing the rounds of a game."""


import logging
import random

from django.db.models import Count, F, Max, Q
from django.utils import timezone
from django.utils.translation import gettext as _ 
from discordcore.members import DiscordMember

from ..constants import MAX_LISTED_PLAYERS, QUIZ_OVER
from ..models import Game, Guess, Question, QuizType, Round
from .guessing import guess_form, round_display
from .guilds import require_host
from .library import require_question_fits
from .scores import game_scores
from .teams import player_label

logger = logging.getLogger(__name__)


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


def open_round(game: Game, host_member: DiscordMember,
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
        raise ValueError(
            _("Reveal the current round before opening the next one."))
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
    return round_payload(queued)


def round_payload(round_: Round) -> dict:
    """Return what the message opening a round shows, with its guess form."""
    display = round_display(round_)
    display['queued'] = queued_count(round_.game)
    display['form'] = guess_form(display)
    display['ping_role_id'] = round_.game.ping_role_id
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


def guess_line(guess: Guess) -> dict:
    """Return how one guess is listed to a host, with its team and answers."""
    player = guess.player
    team = guess.team
    return {'pk': guess.pk,
            'player': player_label(player) if player is not None else '',
            'team': team.name if team is not None else '',
            'text': guess.text, 'secondary_text': guess.secondary_text,
            'text_correct': bool(guess.text_correct),
            'secondary_correct': bool(guess.secondary_correct)}


def round_guesses(round_: Round, with_lines: bool = False) -> dict:
    """Return how many players guessed a round and who was right.

    ``with_lines`` adds the guesses themselves, so a host reads them as they
    come in; the guesses are read once either way.
    """
    guesses = list(round_.guesses.select_related('player', 'team'))
    right = [guess for guess in guesses
             if guess.text_correct and guess.player is not None]
    counts = {'guessed': len(guesses), 'right': len(right),
              'right_names': [guess.player.discord_name or guess.player.username
                              for guess in right][:MAX_LISTED_PLAYERS]}
    if with_lines:
        counts['lines'] = [guess_line(guess) for guess in guesses]
    return counts


CORRECTNESS_FIELDS = {'text': 'text_correct', 'secondary': 'secondary_correct'}
"""The sub-answers of a guess a host may correct, and the flag each one sets."""


def set_guess_correctness(guess: Guess, host_member: DiscordMember,
                          field: str, correct: bool) -> Guess:
    """Mark one sub-answer of a guess right or wrong, as the host reads it.

    A guess the answer matcher missed (a spelling the variants did not cover)
    is corrected here; the points are recomputed from the flags at reveal.
    """
    round_ = guess.round
    game = round_.game
    require_host(game.guild, host_member)
    if not game.is_active:
        raise ValueError(QUIZ_OVER)
    if field not in CORRECTNESS_FIELDS:
        raise ValueError(_("This guess has no such answer."))
    flag = CORRECTNESS_FIELDS[field]
    setattr(guess, flag, bool(correct))
    guess.save(update_fields=[flag])
    logger.info('Game %s round %s: guess %s %s set to %s', game.pk, round_.index,
                guess.pk, flag, bool(correct))
    return guess


def reveal_payload(round_: Round) -> dict:
    """Return what the answer of a revealed round and its standings show."""
    display = round_display(round_)
    display['scores'] = game_scores(round_.game)
    display.update(round_guesses(round_, with_lines=True))
    return display


def reveal_round(round_: Round, host_member: DiscordMember) -> dict:
    """Reveal a round and return its answer with the standings."""
    return reveal_payload(_reveal_round(round_, host_member))


def _reveal_round(round_: Round, host_member: DiscordMember) -> Round:
    """Mark a round as revealed, which ends it for the players."""
    require_host(round_.game.guild, host_member)
    if round_.is_revealed:
        raise ValueError(_("This round is already revealed."))
    round_.revealed_at = timezone.now()
    round_.save(update_fields=['revealed_at'])
    return round_
