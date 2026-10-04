"""The Discord client, started by the ``runbot`` management command."""

import logging
from importlib import import_module
from pathlib import Path

import discord
from django.conf import settings
from discord.ext import commands

logger = logging.getLogger(__name__)

COGS_PACKAGE = 'discordbot.cogs'


def cog_extensions() -> list[str]:
    """Return the import path of every module in ``discordbot/cogs``."""
    directory = Path(import_module(COGS_PACKAGE).__file__).parent
    return [f'{COGS_PACKAGE}.{path.stem}'
            for path in sorted(directory.glob('*.py'))
            if not path.stem.startswith('_')]


class BlindTestBot(commands.Bot):
    """Discord client wired to the Django project's settings and database."""

    def __init__(self) -> None:
        # The members intent backs the web admin's member search, which reads
        # Discord over REST with the bot token. The quiz itself needs nothing
        # else privileged.
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned,
                         intents=intents)

    async def setup_hook(self) -> None:
        """Load the cogs and publish the slash commands before connecting."""
        await self.load_cogs()
        await self.sync_commands()

    async def load_cogs(self) -> None:
        """Load every cog of the ``discordbot.cogs`` package."""
        for extension in cog_extensions():
            await self.load_extension(extension)
            logger.info('Loaded %s', extension)

    async def sync_commands(self) -> None:
        """Publish the slash commands, to the test guilds first when configured."""
        if not settings.TEST_GUILD_IDS:
            synced = await self.tree.sync()
            logger.info('Synced %s slash commands globally', len(synced))
            return
        for guild_id in settings.TEST_GUILD_IDS:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except discord.HTTPException as error:
                logger.warning('Could not sync commands to guild %s: %s',
                               guild_id, error)
            else:
                logger.info('Synced %s slash commands to guild %s',
                            len(synced), guild_id)

    async def on_ready(self) -> None:
        logger.info('Logged in as %s (ID: %s)', self.user, self.user.id)


def create_bot() -> BlindTestBot:
    """Return a bot instance, ready to be started with a token."""
    return BlindTestBot()
