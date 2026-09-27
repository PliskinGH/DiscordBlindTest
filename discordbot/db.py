"""Bridge between the bot's event loop and Django's synchronous ORM."""

from collections.abc import Callable
from typing import Any, TypeVar

from asgiref.sync import sync_to_async
from discord import Interaction, Member, User
from django.db import close_old_connections

from discordcore.models import Guild, Player

T = TypeVar('T')


def _call(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    close_old_connections()
    try:
        return function(*args, **kwargs)
    finally:
        close_old_connections()


async def run_db(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous Django function in the ORM thread and return its result."""
    return await sync_to_async(_call, thread_sensitive=True)(function, *args, **kwargs)


async def guild_for(interaction: Interaction) -> Guild:
    """Return the guild row behind an interaction, creating it when missing."""
    return await run_db(Guild.objects.from_discord, interaction.guild)


async def player_for(user: Member | User) -> Player:
    """Return the player row behind a Discord user, creating it when missing."""
    return await run_db(Player.objects.from_discord, user)


