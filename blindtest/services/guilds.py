"""The Discord servers the bot plays in, their hosts and their defaults."""


from collections.abc import Iterable
import logging

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _ 
from discordcore.cache import STATE_TIMEOUT, forget, guild_row_key, remember
from discordcore.members import (DiscordGuild, DiscordMember, can_manage_guild,
                                 member_mentions)
from discordcore.mentions import normalize_mention, validate_mention
from discordcore.models import Guild, Host, Player

from .. import caching
from ..models import Game

logger = logging.getLogger(__name__)


def is_host(guild: Guild, host_member: DiscordMember) -> bool:
    """Return True when the member may host games in the guild.
    Hosts are the guild's host mentions (a Discord user or a role), plus the
    members Discord allows to manage the server.
    """
    hosts = set(hosts_of(guild))
    return bool(hosts & member_mentions(host_member)) or can_manage_guild(host_member)


def require_host(guild: Guild, host_member: DiscordMember) -> None:
    """Raise PermissionError when the member is not a host of the guild."""
    if not is_host(guild, host_member):
        raise PermissionError(_("Only hosts of this server can run a game. "
                                "Ask an administrator for the host permission."))


def player_of(member: DiscordMember) -> Player | None:
    """Return the player row of a member, or None when they have none yet."""
    return Player.objects.filter(discord_user_id=member.id).first()


def require_admin(member: DiscordMember) -> None:
    """Raise PermissionError unless the member may manage the Discord server."""
    if not can_manage_guild(member):
        raise PermissionError(_("Only server administrators can manage the "
                                "settings of a server."))


def add_host(guild: Guild, mention: str,
             admin_member: DiscordMember) -> Host:
    """Allow a Discord user or role to host games in the guild."""
    require_admin(admin_member)
    host, created = Host.objects.get_or_create(guild=guild,
                                               mention=_checked_mention(mention))
    if created:
        logger.info('%s is now a host of %s', host.mention, guild)
    return host


def remove_host(guild: Guild, mention: str,
                admin_member: DiscordMember) -> Guild:
    """Withdraw the host rights of a Discord user or role in the guild."""
    require_admin(admin_member)
    matches = guild.hosts.filter(mention=_checked_mention(mention))
    if not matches.exists():
        raise ValueError(_("This user or role is not a host of this server."))
    matches.delete()
    return guild


def _checked_mention(mention: str) -> str:
    """Return the canonical mention, refusing one that names no user or role."""
    try:
        validate_mention(mention)
    except ValidationError as error:
        raise ValueError(str(error.messages[0])) from error
    return normalize_mention(mention)


def hosts_of(guild: Guild) -> list[str]:
    """Return the mentions allowed to host games in the guild."""
    return remember(caching.hosts_key(guild),
                    lambda: list(guild.hosts.values_list('mention', flat=True)),
                    STATE_TIMEOUT)


def default_channel_of(guild: Guild) -> int | None:
    """Return the channel games of the guild default to, or None."""
    return guild.default_channel_id


def set_default_channel(guild: Guild, channel_id: int,
                        admin_member: DiscordMember) -> Guild:
    """Make ``channel_id`` the channel the guild's games are played in."""
    require_admin(admin_member)
    guild.default_channel_id = channel_id
    guild.save(update_fields=['default_channel_id'])
    # The cached row would hand the next command the previous default.
    forget(guild_row_key(guild.discord_id))
    logger.info('%s: %s is now the default channel', guild, channel_id)
    return guild


def clear_default_channel(guild: Guild, admin_member: DiscordMember) -> Guild:
    """Send the guild's games back to the channel their host used."""
    require_admin(admin_member)
    guild.default_channel_id = None
    guild.save(update_fields=['default_channel_id'])
    forget(guild_row_key(guild.discord_id))
    logger.info('%s: default channel cleared', guild)
    return guild


def default_ping_role_of(guild: Guild) -> int | None:
    """Return the role pinged for the guild's games."""
    return guild.default_ping_role_id


def set_default_ping_role(guild: Guild, role_id: int,
                          admin_member: DiscordMember) -> Guild:
    """Make ``role_id`` the role the guild's games will ping."""
    require_admin(admin_member)
    guild.default_ping_role_id = role_id
    guild.save(update_fields=['default_ping_role_id'])
    # The cached row would hand the next command the previous default.
    forget(guild_row_key(guild.discord_id))
    logger.info('%s: %s is now the default ping role', guild, role_id)
    return guild


def clear_default_ping_role(guild: Guild, admin_member: DiscordMember) -> Guild:
    """Clear the default guild's ping."""
    require_admin(admin_member)
    guild.default_ping_role_id = None
    guild.save(update_fields=['default_ping_role_id'])
    forget(guild_row_key(guild.discord_id))
    logger.info('%s: default ping role cleared', guild)
    return guild


def target_ping_role_id(guild: Guild, ping_role_id: int | None) -> int | None:
    """Return the role a game pings: the one set, else the guild default.
    No ping if neither is known.
    """
    return ping_role_id if ping_role_id is not None else guild.default_ping_role_id


def target_channel_id(guild: Guild, channel_id: int | None,
                      invoking_id: int | None = None) -> int:
    """Return the channel a game is played in, in the following order of preference.
    The channel the host named, then the channel the server defaults to,
    then the channel the command was run in.
    """
    for candidate in (channel_id, guild.default_channel_id, invoking_id):
        if candidate is not None:
            return candidate
    raise ValueError(_("Give a channel to play this game in."))


def guild_by_discord_id(discord_id: int) -> Guild | None:
    """Return the guild row of a Discord server, or None when it is unknown."""
    return Guild.objects.filter(discord_id=discord_id).first()


def guilds_by_discord_ids(discord_ids: Iterable[int]) -> dict[int, Guild]:
    """Return the guild rows of the Discord IDs given, keyed by Discord ID."""
    return {guild.discord_id: guild
            for guild in Guild.objects.filter(discord_id__in=list(discord_ids))}


def add_guild(discord_guild: DiscordGuild, admin_member: DiscordMember) -> Guild:
    """Register a Discord server the web admin was asked to manage."""
    require_admin(admin_member)
    return Guild.objects.from_discord(discord_guild)


def games_count(guild: Guild) -> int:
    """Return the number of games ever started in the guild."""
    return Game.objects.filter(guild=guild).count()
