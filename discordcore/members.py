"""The parts of Discord objects the domain reads, without importing discord.

Host rights come from the live ``discord.Member`` and not from a local ``Player``
row: role mentions and the "Manage Server" permission only exist on the Discord
side, and a host entry may name a user who never played.
"""

from collections.abc import Iterable
from typing import Any, Protocol

from .mentions import role_mention, user_mention


class DiscordRole(Protocol):
    """The part of a ``discord.Role`` we read."""

    id: int


class DiscordUser(Protocol):
    """The part of a ``discord.User`` we read."""

    id: int
    name: str


class DiscordMember(DiscordUser, Protocol):
    """The part of a ``discord.Member`` the host checks need."""

    roles: Iterable[DiscordRole]


class DiscordGuild(Protocol):
    """The part of a ``discord.Guild`` we read."""

    id: int
    name: str


def member_mentions(member: DiscordMember) -> set[str]:
    """Return the mentions identifying a member: their own and their roles'."""
    mentions = {user_mention(member.id)}
    mentions.update(role_mention(role.id) for role in member.roles)
    return mentions


def can_manage_guild(member: Any) -> bool:
    """Return True when Discord grants the member the "Manage Server" permission."""
    permissions = getattr(member, 'guild_permissions', None)
    return bool(permissions and permissions.manage_guild)
