"""How the players and teams of a game stand."""


from collections.abc import Iterable

from django.db.models import Prefetch

from .. import scoring
from ..models import Game, Guess, Round, Team


def _scored_rounds(game: Game,
                   rounds: Iterable[Round] | None = None) -> list[Round]:
    """Return the rounds a game played, with their guesses, ready to score."""
    if rounds is None:
        rounds = game.rounds.filter(started_at__isnull=False).prefetch_related(
            Prefetch('guesses', queryset=Guess.objects
                     .select_related('player', 'team')
                     .order_by('submitted_at', 'pk')))
    return list(rounds)


def _team_row(team: Team) -> dict:
    """Return the leaderboard row of a team."""
    return {'label': team.name, 'name': team.name, 'kind': 'team', 'points': 0}


def _player_row(player) -> dict:
    """Return the leaderboard row of a player who guessed outside a team."""
    return {'label': player.discord_name or player.username,
            'username': player.username, 'discord_name': player.discord_name,
            'kind': 'player', 'points': 0}


def _row_for(guess: Guess, rows: dict) -> dict | None:
    """Return the row a guess scores for, or None when it scores for nobody.

    A guess counts for the team it was made in, or for its player when it was
    made outside a team: a team's points are not also its players'.
    """
    if guess.team_id:
        key = ('team', guess.team_id)
    elif guess.player_id is not None:
        key = ('player', guess.player_id)
    else:
        return None
    row = rows.get(key)
    if row is None:
        row = rows[key] = (_team_row(guess.team) if guess.team_id
                           else _player_row(guess.player))
    return row


def game_scores(game: Game,
                rounds: Iterable[Round] | None = None) -> list[dict]:
    """Return the leaderboard of a game, best first.

    One leaderboard holds the teams and the players who guessed outside one, so
    a game mixing them still has a single ranking. A caller that read the rounds
    of the game already passes them in.
    """
    rows: dict[tuple[str, int], dict] = {}
    for round_ in _scored_rounds(game, rounds):
        guesses = list(round_.guesses.all())
        points = scoring.round_points(round_, guesses)
        for guess in guesses:
            row = _row_for(guess, rows)
            if row is not None:
                row['points'] += points.get(guess.pk, 0)
    return sorted(rows.values(), key=lambda row: (-row['points'], row['label']))
