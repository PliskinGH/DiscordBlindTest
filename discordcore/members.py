"""The member shapes the domain reads: real Discord objects or local ones.

The bot hands the domain a real ``discord.Member``; the web admin has no client
and hands it a :class:`LocalMember`. Both go through the same functions, so each
annotation names the union of the two and no shape is described twice.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Union

from .mentions import role_mention, user_mention

if TYPE_CHECKING:
    import discord


@dataclass(frozen=True)
class LocalRole:
    """A plain role, for a caller without a ``discord.Role``."""

    id: int


@dataclass(frozen=True)
class LocalPermissions:
    """The permissions read here, for a caller without ``discord.Permissions``."""

    manage_guild: bool = False


@dataclass(frozen=True)
class LocalUser:
    """A plain Discord account, for a caller without a ``discord.User``."""

    id: int
    name: str


@dataclass(frozen=True)
class LocalMember(LocalUser):
    """A plain member, for a caller without a ``discord.Member``."""

    roles: Iterable[LocalRole] = ()
    guild_permissions: LocalPermissions = field(default_factory=LocalPermissions)


@dataclass(frozen=True)
class LocalGuild:
    """A plain server, for a caller without a ``discord.Guild``."""

    id: int
    name: str


# What the domain accepts: the real Discord object or its local stand-in.
DiscordUser = Union['discord.User', LocalUser]
DiscordMember = Union['discord.Member', LocalMember]
DiscordGuild = Union['discord.Guild', LocalGuild]


def member_mentions(member: DiscordMember) -> set[str]:
    """Return the mentions identifying a member: their own and their roles'."""
    mentions = {user_mention(member.id)}
    mentions.update(role_mention(role.id) for role in member.roles)
    return mentions


def can_manage_guild(member: DiscordMember) -> bool:
    """Return True when Discord grants the member the "Manage Server" permission."""
    permissions = getattr(member, 'guild_permissions', None)
    return bool(permissions and permissions.manage_guild)
