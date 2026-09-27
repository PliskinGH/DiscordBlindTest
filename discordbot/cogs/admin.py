"""Slash commands guild administrators use to manage their hosts."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from blindtest import services
from discordcore.mentions import role_mention, user_mention

from ..db import guild_for, run_db

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
                              description='Who may run blind tests here.',
                              parent=group)

    @app_commands.default_permissions(manage_guild=True)
    @host.command(name='add',
                  description='Allow a user or a role to host blind tests.')
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
                await run_db(services.add_host, guild, mention,
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
        """Drop the given host mentions from the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            mentions = _target_mentions(user, role)
            if not mentions:
                await interaction.followup.send(
                    'Give a user or a role to remove.', ephemeral=True)
                return
            for mention in mentions:
                await run_db(services.remove_host, guild, mention,
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
    @host.command(name='list', description='Show who may run blind tests here.')
    async def host_list(self, interaction: discord.Interaction) -> None:
        """List the host mentions registered for the server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            mentions = await run_db(services.hosts_of, guild)
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


async def setup(bot: commands.Bot) -> None:
    """Load the cog; called by the bot when the extension is loaded."""
    await bot.add_cog(AdminCog(bot))