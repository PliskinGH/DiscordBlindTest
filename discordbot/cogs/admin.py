"""Slash commands guild administrators use to manage their server."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discordcore.mentions import role_mention, user_mention
from blindtest.services.guilds import (add_host, clear_default_channel,
                                       clear_default_ping_role,
                                       default_channel_of,
                                       default_ping_role_of, hosts_of,
                                       remove_host, set_default_channel,
                                       set_default_ping_role)

from ..db import guild_for, run_db
from .game import GameChannel, require_pingable

logger = logging.getLogger(__name__)


def _target_mentions(user: discord.User | None,
                     role: discord.Role | None) -> list[str]:
    """Return the mentions of the targets given to a command."""
    mentions: list[str] = []
    if user is not None:
        mentions.append(user_mention(user.id))
    if role is not None:
        mentions.append(role_mention(role.id))
    return mentions


class AdminCog(commands.Cog):
    """Host management commands, reserved to server administrators."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(name='admin',
                               description='Manage this server (administrators).')
    host = app_commands.Group(name='host',
                              description='Who may run games here.',
                              parent=group)
    channel = app_commands.Group(
        name='channel',
        description='Where the games of this server are played.',
        parent=group)
    ping = app_commands.Group(
        name='ping',
        description='The role the games of this server will ping by default.',
        parent=group)

    @app_commands.default_permissions(manage_guild=True)
    @host.command(name='add',
                  description='Allow a user or a role to host games.')
    @app_commands.describe(user='User allowed to host.',
                           role='Role allowed to host.')
    async def host_add(self, interaction: discord.Interaction,
                       user: discord.User | None = None,
                       role: discord.Role | None = None) -> None:
        """Register the given host mentions for the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            mentions = _target_mentions(user, role)
            if not mentions:
                await interaction.followup.send(
                    'Give a user or a role to add.', ephemeral=True)
                return
            for mention in mentions:
                await run_db(add_host, guild, mention,
                             interaction.user)
            await interaction.followup.send(
                f'Now hosting: {", ".join(mentions)}.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to add a host')
            await interaction.followup.send(
                'Could not add the host.', ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @host.command(name='remove',
                  description='Withdraw the host rights of a user or a role.')
    @app_commands.describe(user='User to remove.', role='Role to remove.')
    async def host_remove(self, interaction: discord.Interaction,
                          user: discord.User | None = None,
                          role: discord.Role | None = None) -> None:
        """Remove the given host mentions from the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            mentions = _target_mentions(user, role)
            if not mentions:
                await interaction.followup.send(
                    'Give a user or a role to remove.', ephemeral=True)
                return
            for mention in mentions:
                await run_db(remove_host, guild, mention,
                             interaction.user)
            await interaction.followup.send(
                f'No longer hosting: {", ".join(mentions)}.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to remove a host')
            await interaction.followup.send(
                'Could not remove the host.', ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @host.command(name='list', description='Show who may run games here.')
    async def host_list(self, interaction: discord.Interaction) -> None:
        """List the host mentions registered for the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            mentions = await run_db(hosts_of, guild)
            notice = 'Members with "Manage Server" can always host.'
            if not mentions:
                await interaction.followup.send(
                    f'No host is registered. {notice}', ephemeral=True)
                return
            await interaction.followup.send(
                f'Hosts: {", ".join(mentions)}\n{notice}', ephemeral=True)
        except Exception:
            logger.exception('Failed to list the hosts')
            await interaction.followup.send(
                'Could not list the hosts.', ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @channel.command(name='set',
                     description='Play the games of this server here.')
    @app_commands.describe(channel='Channel to play in; this one by default.')
    async def channel_set(self, interaction: discord.Interaction,
                          channel: GameChannel | None = None) -> None:
        """Make a channel the default one of the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            target = channel or interaction.channel
            await run_db(set_default_channel, guild, target.id,
                         interaction.user)
            await interaction.followup.send(
                f'Games are now played in {target.mention}.', ephemeral=True)
        except PermissionError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to set the default channel')
            await interaction.followup.send(
                'Could not set the default channel.', ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @channel.command(name='clear',
                     description='Play the games where they are started.')
    async def channel_clear(self, interaction: discord.Interaction) -> None:
        """Clear the default channel of the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            await run_db(clear_default_channel, guild, interaction.user)
            await interaction.followup.send(
                'Games are now played where they are started.',
                ephemeral=True)
        except PermissionError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to clear the default channel')
            await interaction.followup.send(
                'Could not clear the default channel.', ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @channel.command(
        name='show',
        description='Show where the games of this server are played.')
    async def channel_show(self, interaction: discord.Interaction) -> None:
        """Report the default channel of the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            channel_id = await run_db(default_channel_of, guild)
            if channel_id is None:
                await interaction.followup.send(
                    'No default channel: a game is played where it is started.',
                    ephemeral=True)
                return
            await interaction.followup.send(
                f'Games are played in <#{channel_id}>.', ephemeral=True)
        except Exception:
            logger.exception('Failed to show the default channel')
            await interaction.followup.send(
                'Could not show the default channel.', ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @ping.command(name='set',
                  description='Ping this role when a game opens.')
    @app_commands.describe(role='Role to ping when a game starts or a round opens.')
    async def ping_set(self, interaction: discord.Interaction,
                       role: discord.Role) -> None:
        """Make a role the one the server's games ping."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            require_pingable(interaction, role)
            await run_db(set_default_ping_role, guild, role.id,
                         interaction.user)
            await interaction.followup.send(
                f'Games now ping {role.mention}.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to set the default ping role')
            await interaction.followup.send(
                'Could not set the default ping role.', ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @ping.command(name='clear',
                  description='Clear the default ping role for games.')
    async def ping_clear(self, interaction: discord.Interaction) -> None:
        """Clear the default ping role of the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            await run_db(clear_default_ping_role, guild,
                         interaction.user)
            await interaction.followup.send(
                'Games now ping nobody.', ephemeral=True)
        except PermissionError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to clear the default ping role')
            await interaction.followup.send(
                'Could not clear the default ping role.', ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @ping.command(
        name='show',
        description='Show the role the games of this server ping by default.')
    async def ping_show(self, interaction: discord.Interaction) -> None:
        """Report the default ping role of the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            role_id = await run_db(default_ping_role_of, guild)
            if role_id is None:
                await interaction.followup.send(
                    'No default ping role: a game pings nobody.', ephemeral=True)
                return
            await interaction.followup.send(
                f'Games ping <@&{role_id}>.', ephemeral=True)
        except Exception:
            logger.exception('Failed to show the default ping role')
            await interaction.followup.send(
                'Could not show the default ping role.', ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Load the cog; called by the bot when the extension is loaded."""
    await bot.add_cog(AdminCog(bot))
