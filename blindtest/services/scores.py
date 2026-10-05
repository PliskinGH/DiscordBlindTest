"""How the players of a game stand."""


from collections.abc import Iterable

from django.db.models import Prefetch

from .. import scoring
from ..models import Game, Guess, Round


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
