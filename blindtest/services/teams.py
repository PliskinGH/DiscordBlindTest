"""The teams guessing together in a game, and the players in them."""


from collections.abc import Iterable
import logging

from django.db.models import Q
from django.utils.translation import gettext as _
from discordcore.cache import LIST_TIMEOUT, remember
from discordcore.members import DiscordMember
from discordcore.models import Guild, Player

from .. import caching
from ..constants import MAX_CHOICES
from ..models import Game, Round, Team
from .guilds import require_host

logger = logging.getLogger(__name__)


def player_label(player: Player) -> str:
    """Return how a player is shown in a team."""
    return player.discord_name or player.username


def _require_editable(game: Game) -> None:
    """Refuse a change to the teams of a game that has finished."""
    if not game.is_active:
        raise ValueError(_("This game is over: its teams cannot change."))


def _checked_name(game: Game, name: str, current: Team | None = None) -> str:
    """Return the name a team of the game may take, refusing a taken one.

    The comparison ignores case, since the constraint behind it does not: a host
    would otherwise be able to hold two teams called "Reds" and "reds".
    """
    name = name.strip()
    if not name:
        raise ValueError(_("Give the team a name."))
    taken = game.teams.filter(name__iexact=name)
    if current is not None:
        taken = taken.exclude(pk=current.pk)
    if taken.exists():
        raise ValueError(_('This game already has a team called "%(name)s".')
                         % {'name': name})
    return name


def teams_of(game: Game) -> list[dict]:
    """Return the teams of a game with their players, as the pages list them."""
    return remember(caching.teams_key(game), lambda: _team_rows(game),
                    LIST_TIMEOUT)


def _team_rows(game: Game) -> list[dict]:
    """Read the teams of a game with their players straight from the database."""
    return [{'pk': team.pk, 'name': team.name,
             'players': [{'pk': player.pk, 'label': player_label(player)}
                         for player in team.players.all()]}
            for team in game.teams.prefetch_related('players').all()]


def team_by_pk(game: Game, pk: str | int) -> Team:
    """Return the team of a game picked by its pk, refusing a pk it has none.

    A picker offers the label of a choice but sends its value, so a team named
    from Discord arrives as its pk rather than as the name shown beside it.
    """
    try:
        wanted = int(str(pk).strip())
    except (TypeError, ValueError):
        wanted = None
    team = game.teams.filter(pk=wanted).first() if wanted is not None else None
    if team is None:
        raise ValueError(_('This game has no team with that id.'))
    return team


def team_choices(game: Game, text: str = '',
                 limit: int = MAX_CHOICES) -> list[dict]:
    """Return the teams of a game whose name matches, as a picker offers them."""
    teams = game.teams.all()
    if text.strip():
        teams = teams.filter(name__icontains=text.strip())
    return [{'pk': team.pk, 'label': team.name} for team in teams[:limit]]


def copyable_team_option(team: Team) -> str:
    """Return how a host picks a team to copy: the game it played in, and its name."""
    return f'{team.game.display_name} — {team.name}'


def copyable_teams(guild: Guild, current: Game | None = None, text: str = '',
                   limit: int = MAX_CHOICES) -> list[dict]:
    """Return the teams of a guild's other games a host may copy.

    The game a team played in is searchable as well as its name, since a host
    remembers "the Reds of last month" rather than which game that was.
    """
    teams = _copyable_teams(guild, current)
    wanted = text.strip()
    if wanted:
        teams = teams.filter(Q(name__icontains=wanted)
                             | Q(game__name__icontains=wanted))
    return [{'pk': team.pk, 'label': copyable_team_option(team)}
            for team in teams[:limit]]


def copyable_team_queryset(guild: Guild, current: Game | None = None):
    """Return the teams a host may copy, for the web admin to search."""
    return _copyable_teams(guild, current).order_by('-game__created_at', 'name')


def _copyable_teams(guild: Guild, current: Game | None):
    """Return the teams of the games of a guild but one."""
    teams = Team.objects.filter(game__guild=guild).select_related('game')
    return teams.exclude(game=current) if current is not None else teams


def team_of(game: Game, player: Player) -> Team | None:
    """Return the team of the player in this game, if any."""
    return player.teams.filter(game=game).first()


def team_at_round(round_: Round, player: Player) -> Team | None:
    """Return the team of a player in the game of a round, if any.

    Read uncached: a stale one would score a guess for the wrong team, and teams
    are moved between rounds.
    """
    return Team.objects.filter(game_id=round_.game_id, players=player).first()


def _assign(team: Team, player: Player) -> None:
    """Put a player in a team, moving them out of their other team of the game."""
    other = team_of(team.game, player)
    if other is not None and other.pk != team.pk:
        other.players.remove(player)
    team.players.add(player)


def add_team(game: Game, host_member: DiscordMember, name: str,
             players: Iterable[Player] = ()) -> Team:
    """Create a team in a game, optionally with its first players."""
    require_host(game.guild, host_member)
    _require_editable(game)
    team = Team.objects.create(game=game, name=_checked_name(game, name))
    for player in players:
        _assign(team, player)
    logger.info('Game %s: team %s created', game.pk, team.name)
    return team


def assign_player(team: Team, host_member: DiscordMember, player: Player) -> Team:
    """Put a player in a team, moving them out of their other team of the game."""
    require_host(team.game.guild, host_member)
    _require_editable(team.game)
    _assign(team, player)
    return team


def assign_players(team: Team, host_member: DiscordMember,
                   players: Iterable[Player]) -> Team:
    """Put several players in a team, moving them out of their other teams."""
    require_host(team.game.guild, host_member)
    _require_editable(team.game)
    for player in players:
        _assign(team, player)
    return team


def remove_player(team: Team, host_member: DiscordMember, player: Player) -> Team:
    """Take a player out of a team."""
    require_host(team.game.guild, host_member)
    _require_editable(team.game)
    if not team.players.filter(pk=player.pk).exists():
        raise ValueError(_('%(name)s does not play for "%(team)s".')
                         % {'name': player_label(player), 'team': team.name})
    team.players.remove(player)
    logger.info('Game %s: %s left team %s', team.game_id, player, team.name)
    return team


def rename_team(team: Team, host_member: DiscordMember, name: str) -> Team:
    """Give a team another name."""
    require_host(team.game.guild, host_member)
    _require_editable(team.game)
    previous = team.name
    team.name = _checked_name(team.game, name, current=team)
    team.save(update_fields=['name'])
    logger.info('Game %s: team %s renamed to %s', team.game_id, previous,
                team.name)
    return team


def remove_team(team: Team, host_member: DiscordMember) -> None:
    """Remove a team of a game that has not scored yet.

    The guesses of a team are deleted with it, so a team that has already been
    scored for is kept rather than taking those points out of the standings.
    """
    require_host(team.game.guild, host_member)
    _require_editable(team.game)
    if team.guesses.exists():
        raise ValueError(_("This team has already scored and cannot be "
                           "removed."))
    name, game_id = team.name, team.game_id
    team.delete()
    logger.info('Game %s: team %s removed', game_id, name)


def copy_team(game: Game, host_member: DiscordMember, source: Team) -> Team:
    """Create a team of a game from a team of another game, players included.

    The name is checked as any other team's is: a game with a "Reds" already
    refuses the copy rather than ending up with a second team of the same name.
    """
    require_host(game.guild, host_member)
    if source.game.guild_id != game.guild_id:
        raise ValueError(_("This team belongs to another server."))
    if source.game_id == game.pk:
        raise ValueError(_("Pick a team of another game to copy from."))
    team = add_team(game, host_member, source.name, source.players.all())
    logger.info('Game %s: team %s copied from game %s', game.pk, team.name,
                source.game_id)
    return team


def copy_team_by_pk(game: Game, host_member: DiscordMember,
                    source_pk: int) -> Team:
    """Create a team of a game from a team of another game, named by its pk."""
    source = Team.objects.filter(pk=int(source_pk)).first()
    if source is None:
        raise ValueError(_("This team no longer exists."))
    return copy_team(game, host_member, source)