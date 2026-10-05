"""The answer form of a round, the guesses sent to it, the teams."""


from collections.abc import Iterable
import logging

from django.utils.translation import gettext as _ 
from discordcore.cache import LIST_TIMEOUT, remember
from discordcore.members import DiscordMember
from discordcore.models import Player

from .. import caching
from .. import matching
from ..constants import MAX_CHOICES
from ..models import (Answer, Game, Guess, Question, QuizType, Round,
                      ScoringMode, Team)
from .guilds import require_host
from .library import question_line

logger = logging.getLogger(__name__)


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


def _team_for(round_: Round, player: Player) -> Team | None:
    """Return the team of a player in the game of a round, if any."""
    return Team.objects.filter(game_id=round_.game_id, players=player).first()


def _answer_forms(answer: Answer | None) -> list[str]:
    """Return the comparable forms of an answer, variants included."""
    if answer is None:
        return []
    forms = {matching.normalize(answer.text)}
    forms.update(matching.normalize(variant.text)
                 for variant in answer.variants.all())
    forms.discard('')
    return sorted(forms)


def accepted_forms(question: Question) -> dict[str, list[str]]:
    """Return the forms each answer of a question is accepted in.
    Every guess of a round is matched against these, so they are read once and
    forgotten as soon as an answer or a variant of it is edited.
    """
    return remember(caching.answer_forms_key(question),
                    lambda: {'expected': _answer_forms(question.expected_answer),
                             'secondary': _answer_forms(question.secondary_answer)},
                    LIST_TIMEOUT)


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
    # The team is read uncached: a stale one would score a guess for the wrong
    # team, and teams are moved between rounds.
    team = _team_for(round_, player)
    question = (Question.objects
                .select_related('expected_answer', 'secondary_answer')
                .get(pk=round_.question_id))
    forms = accepted_forms(question)
    guess = Guess.objects.create(
        round=round_, player=player, team=team,
        text=text, secondary_text=secondary_text,
        text_correct=matching.matches_normalized(text, forms['expected']),
        secondary_correct=matching.matches_normalized(secondary_text,
                                                      forms['secondary']))
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


def guess_of(round_: Round, player: Player) -> Guess | None:
    """Return what the player answered in a round, if they answered at all."""
    return round_.guesses.filter(player=player).first()
