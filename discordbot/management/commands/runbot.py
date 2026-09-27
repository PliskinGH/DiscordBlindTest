"""Management command starting the Discord bot in the Django process."""

import logging
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from discordbot.bot import create_bot

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run the Discord bot.'

    def handle(self, *args: Any, **options: Any) -> None:
        token = settings.DISCORD_TOKEN
        if not token:
            raise CommandError('DISCORD_TOKEN is not set: add it to your .env file.')
        bot = create_bot()
        self.stdout.write('Starting the bot, press Ctrl+C to stop.')
        # log_handler=None keeps discord.py from replacing Django's logging setup.
        bot.run(token, log_handler=None)
