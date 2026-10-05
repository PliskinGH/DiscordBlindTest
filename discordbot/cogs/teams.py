"""Slash commands a host uses to fill the teams of a running game."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from blindtest.services.games import active_game
from blindtest.services.teams import (add_team, assign_player, copyable_teams,
                                      copy_team_by_pk, remove_player,
                                      remove_team, rename_team,
                                      team_by_pk, team_choices, teams_of)

from ..db import guild_for, player_for, run_db
from ..ui import option_label

logger = logging.getLogger(__name__)

# Said wherever a command needs the game of the server and finds none.
NO_GAME = 'No game is running in this server.'

TEAM_OPTION = {'team': 'Team of this game.'}
MEMBER_OPTION = {'member': 'Member to move.'}


async def team_autocomplete(
        interaction: discord.Interaction,
        current: str) -> list[app_commands.Choice[str]]:
    """Offer the teams of the game being played in this server."""
    try:
        guild = await guild_for(interaction)
        game = await run_db(active_game, guild)
        if game is None:
            return []
        teams = await run_db(team_choices, game, current)
        return [app_commands.Choice(name=option_label(team),
                                    value=str(team['pk']))
                for team in teams]
    except Exception:
        logger.exception('Failed to offer team choices')
        return []


async def copyable_team_autocomplete(
        interaction: discord.Interaction,
        current: str) -> list[app_commands.Choice[str]]:
    """Offer the teams of the other games this server has played.

    Discord does not tell an autocomplete what the host already picked for
    another option, and there is no other option here: the target is always the
    game being played, so every team of another game is a possible source.
    """
    try:
        guild = await guild_for(interaction)
        game = await run_db(active_game, guild)
        teams = await run_db(copyable_teams, guild, game, current)
        return [app_commands.Choice(name=option_label(team),
                                    value=str(team['pk']))
                for team in teams]
    except Exception:
        logger.exception('Failed to offer the teams to copy')
        return []


class TeamsCog(commands.Cog):
    """Team management commands, reserved to the hosts of a game."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name='teams',
        description='The teams answering together in the game of this server.')
    members = app_commands.Group(
        name='members',
        description='Who answers in a team of this game.',
        parent=group)

    async def _game_of(self, interaction: discord.Interaction):
        """Return the game of the server, refusing when none is running."""
        guild = await guild_for(interaction)
        game = await run_db(active_game, guild)
        if game is None:
            raise ValueError(NO_GAME)
        return game

    async def _respond(self, interaction: discord.Interaction,
                       work, failing: str) -> None:
        """Run a team operation, reporting its note or why it refused."""
        try:
            note = await work()
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception(failing)
            await interaction.followup.send(failing, ephemeral=True)
        else:
            await interaction.followup.send(note, ephemeral=True)

    @group.command(name='add', description='Create a team for this game.')
    @app_commands.describe(name='Name of the team, e.g. Reds.',
                           member='First member of the team.')
    async def team_add(self, interaction: discord.Interaction, name: str,
                       member: discord.Member | None = None) -> None:
        """Create a team, optionally with its first member."""
        await interaction.response.defer(ephemeral=True)
        await self._respond(interaction, lambda: self._add(interaction, name,
                                                          member),
                            'Could not add the team.')

    async def _add(self, interaction: discord.Interaction, name: str,
                   member: discord.Member | None) -> str:
        """Create the team and return the note to send back."""
        game = await self._game_of(interaction)
        players = [] if member is None else [await player_for(member)]
        team = await run_db(add_team, game, interaction.user, name, players)
        return f'{team.name} created.'

    @members.command(name='add',
                      description='Put a member in a team of this game.')
    @app_commands.describe(**TEAM_OPTION, **MEMBER_OPTION)
    @app_commands.autocomplete(team=team_autocomplete)
    async def member_add(self, interaction: discord.Interaction, team: str,
                         member: discord.Member) -> None:
        """Move a member into the team the host named."""
        await interaction.response.defer(ephemeral=True)
        await self._respond(
            interaction,
            lambda: self._assign(interaction, team, member, adding=True),
            'Could not add the member to the team.')

    @members.command(name='remove',
                      description='Take a member out of a team of this game.')
    @app_commands.describe(team='Team to take the member out of.',
                           member='Member to take out of the team.')
    @app_commands.autocomplete(team=team_autocomplete)
    async def member_remove(self, interaction: discord.Interaction,
                            team: str, member: discord.Member) -> None:
        """Take a member out of the team the host named."""
        await interaction.response.defer(ephemeral=True)
        await self._respond(
            interaction,
            lambda: self._assign(interaction, team, member, adding=False),
            'Could not take the member out of the team.')

    async def _assign(self, interaction: discord.Interaction, team: str,
                      member: discord.Member, adding: bool) -> str:
        """Move a member in or out of the team the host picked."""
        named = await run_db(team_by_pk, await self._game_of(interaction),
                             team)
        player = await player_for(member)
        await run_db(assign_player if adding else remove_player,
                     named, interaction.user, player)
        label = player.discord_name or player.username
        return (f'{label} {"joins" if adding else "leaves"} {named.name}.')

    @group.command(name='copy',
                   description='Copy a team of a game this server played before.')
    @app_commands.describe(team='Team to copy, named after the game it played in.')
    @app_commands.autocomplete(team=copyable_team_autocomplete)
    async def team_copy(self, interaction: discord.Interaction,
                        team: str) -> None:
        """Add a team of another game to the one being played, players included."""
        await interaction.response.defer(ephemeral=True)
        await self._respond(interaction, lambda: self._copy(interaction, team),
                            'Could not copy the team.')

    async def _copy(self, interaction: discord.Interaction, team: str) -> str:
        """Copy the team and return the note to send back."""
        game = await self._game_of(interaction)
        copied = await run_db(copy_team_by_pk, game, interaction.user, team)
        return f'{copied.name} copied, with its members.'

    @group.command(name='rename', description='Give a team another name.')
    @app_commands.describe(team='Team to rename.', name='Its new name.')
    @app_commands.autocomplete(team=team_autocomplete)
    async def team_rename(self, interaction: discord.Interaction, team: str,
                          name: str) -> None:
        """Rename the team the host named."""
        await interaction.response.defer(ephemeral=True)
        await self._respond(interaction, lambda: self._rename(interaction, team,
                                                             name),
                            'Could not rename the team.')

    async def _rename(self, interaction: discord.Interaction, team: str,
                      name: str) -> str:
        """Rename the team and return the note to send back."""
        named = await run_db(team_by_pk, await self._game_of(interaction),
                             team)
        renamed = await run_db(rename_team, named, interaction.user, name)
        return f'The team is now {renamed.name}.'

    @group.command(name='remove', description='Remove a team of this game.')
    @app_commands.describe(**TEAM_OPTION)
    @app_commands.autocomplete(team=team_autocomplete)
    async def team_remove(self, interaction: discord.Interaction,
                          team: str) -> None:
        """Remove the team the host named, which cannot have scored."""
        await interaction.response.defer(ephemeral=True)
        await self._respond(interaction, lambda: self._remove(interaction, team),
                            'Could not remove the team.')

    async def _remove(self, interaction: discord.Interaction, team: str) -> str:
        """Remove the team and return the note to send back."""
        named = await run_db(team_by_pk, await self._game_of(interaction),
                             team)
        name = named.name
        await run_db(remove_team, named, interaction.user)
        return f'{name} removed.'

    @group.command(name='list', description='List the teams of this game.')
    async def team_list(self, interaction: discord.Interaction) -> None:
        """Report the teams of the game and who answers in them."""
        await interaction.response.defer(ephemeral=True)
        await self._respond(interaction, lambda: self._list(interaction),
                            'Could not list the teams.')

    async def _list(self, interaction: discord.Interaction) -> str:
        """Return the roster of the game as one message."""
        teams = await run_db(teams_of, await self._game_of(interaction))
        if not teams:
            return 'This game has no team: its points are counted per player.'
        return '\n'.join(f'**{team["name"]}**: {self._members(team["players"])}'
                         for team in teams)

    @staticmethod
    def _members(players: list[dict]) -> str:
        """Return the members of a team as one line of names."""
        return ', '.join(player['label'] for player in players) or 'nobody'


async def setup(bot: commands.Bot) -> None:
    """Load the cog; called by the bot when the extension is loaded."""
    await bot.add_cog(TeamsCog(bot))

    async def _add(self, interaction: discord.Interaction, name: str,
                   member: discord.Member | None) -> str:
        """Create the team and return the note to send back."""
        game = await self._game_of(interaction)
        players = [] if member is None else [await player_for(member)]
        team = await run_db(add_team, game, interaction.user, name, players)
        return f'{team.name} created.'