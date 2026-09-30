"""Cache scopes, keys and limits of the blind test data.

A guild plays the questions of two libraries, its own and the global one, so the
keys of its lists carry the version of both and an edit of either is seen at once.
"""

from discordcore.cache import guild_scope, scoped
from discordcore.models import Guild

from .models import Game

# Rows a rendered list may hold before its query is left to run every time.
LIBRARY_CACHE_LIMIT = 1000
QUEUE_CACHE_LIMIT = 1000
GAME_CACHE_LIMIT = 200
# Library 0 is the global one, which every guild plays beside its own.
GLOBAL_LIBRARY_ID = 0


def library_scope(guild_id: int | None) -> str:
    """Return the scope of a library, the global one counting as library 0."""
    return f'lib:{guild_id or GLOBAL_LIBRARY_ID}'


def rounds_scope(guild_id: int) -> str:
    """Return the scope of every round played in a guild."""
    return f'rounds:{guild_id}'


def hosts_key(guild: Guild) -> str:
    """Return the key of the host mentions of a guild."""
    return scoped('bt:hosts', guild_scope(guild.pk))


def library_options_key(guild: Guild) -> str:
    """Return the key of the rendered options of the libraries a guild plays."""
    return scoped('bt:options', library_scope(guild.pk), library_scope(None))


def used_questions_key(game: Game) -> str:
    """Return the key of the questions a game already queued or played."""
    return scoped('bt:used', rounds_scope(game.guild_id))


def queued_options_key(game: Game) -> str:
    """Return the key of the rendered options of the rounds a game queued."""
    return scoped('bt:queued', rounds_scope(game.guild_id),
                  library_scope(game.guild_id), library_scope(None))


def game_options_key(guild: Guild) -> str:
    """Return the key of the rendered options of the games of a guild."""
    return scoped('bt:games', guild_scope(guild.pk), rounds_scope(guild.pk))
