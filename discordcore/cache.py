"""Cache helpers the domain layer reads its derived data through.

An entry is keyed by the version counters of the scopes it depends on: a write
bumps those counters, so the next read builds a new key and the entry it leaves
behind expires on its own. Counters are seeded with the clock, which keeps one
lost with the cache from handing back an entry built from an older counter.
"""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from django.core.cache import cache

logger = logging.getLogger(__name__)

VERSION_PREFIX = 'bt:v'
# Counters outlive every entry they invalidate.
COUNTER_TIMEOUT = None

# An entry whose staleness would be a wrong permission or state.
STATE_TIMEOUT = 60
# An entry rebuilt for a list read between writes, where staleness only shows an
# option a host added a moment ago.
LIST_TIMEOUT = 3600

# A cache that stopped answering would otherwise report every single call.
FAILURE_INTERVAL = 60
_last_failure = 0.0

T = TypeVar('T')


def _report_failure(message: str, *args) -> None:
    """Report a cache failure, at most once a minute."""
    global _last_failure
    now = time.monotonic()
    if now - _last_failure < FAILURE_INTERVAL:
        return
    _last_failure = now
    logger.error(message, *args, exc_info=True)


def _counter_name(scope: str) -> str:
    """Return the cache key holding the version of a scope."""
    return f'{VERSION_PREFIX}:{scope}'


def _seed(scope: str, name: str) -> int:
    """Return the counter of a scope read for the first time."""
    try:
        return cache.get_or_set(name, int(time.time()), COUNTER_TIMEOUT)
    except Exception:
        _report_failure('Could not seed the %s cache scope', scope)
        return 0


def scope_versions(*scopes: str) -> list[int]:
    """Return the version counter of each scope, in one read."""
    names = [_counter_name(scope) for scope in scopes]
    try:
        found = cache.get_many(names)
    except Exception:
        _report_failure('Could not read the cache scopes %s', ', '.join(scopes))
        return [0] * len(scopes)
    return [found[name] if name in found else _seed(scope, name)
            for scope, name in zip(scopes, names)]


def bump(*scopes: str) -> None:
    """Invalidate every key built from the scopes.

    A cache that cannot be reached loses the invalidation rather than the write;
    the lifetime of an entry then decides how long it stays stale.
    """
    for scope in scopes:
        name = _counter_name(scope)
        try:
            cache.incr(name)
        except ValueError:
            cache.set(name, int(time.time()), COUNTER_TIMEOUT)
        except Exception:
            _report_failure('Could not invalidate the %s cache scope', scope)


def scoped(prefix: str, *scopes: str) -> str:
    """Return a key of a prefix and the versions of the scopes it depends on."""
    versions = (f'{scope}={version}' for scope, version
                in zip(scopes, scope_versions(*scopes)))
    return ':'.join([prefix, *versions])


def remember(cache_key: str, producer: Callable[[], T], timeout: int) -> T:
    """Return the cached value, building it with ``producer`` when missing.

    A cache that cannot be reached falls back to the producer, which costs
    queries instead of failing the command.
    """
    try:
        value = cache.get(cache_key)
    except Exception:
        _report_failure('Could not read %s from the cache', cache_key)
        return producer()
    if value is not None:
        return value
    return store(cache_key, producer(), timeout)


def store(cache_key: str, value: T, timeout: int) -> T:
    """Cache a value and return it, for a caller that read it already."""
    try:
        cache.set(cache_key, value, timeout)
    except Exception:
        _report_failure('Could not store %s in the cache', cache_key)
    return value


def forget(cache_key: str) -> None:
    """Drop a cached entry; an unreachable cache loses the drop, not the write."""
    try:
        cache.delete(cache_key)
    except Exception:
        _report_failure('Could not drop %s from the cache', cache_key)


def guild_scope(guild_id: int) -> str:
    """Return the scope of the hosts and game lists of a guild."""
    return f'guild:{guild_id}'


def guild_row_key(discord_id: int) -> str:
    """Return the key of a guild row; its name is compared, never keyed."""
    return f'bt:guild:{discord_id}'


def player_row_key(discord_user_id: int) -> str:
    """Return the key of a player row; its name is compared, never keyed."""
    return f'bt:player:{discord_user_id}'
