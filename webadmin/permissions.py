"""The Discord member a logged in player is, rebuilt from the OAuth session."""

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils.translation import gettext_lazy as _

from discordcore.members import LocalMember, LocalPermissions, LocalRole
from discordcore.models import Guild
from blindtest.services.guilds import (guild_by_discord_id, require_admin,
                                       require_host)

from . import discord_api

logger = logging.getLogger(__name__)


def can_manage(request, discord_guild_id: int) -> bool:
    """Return True when the OAuth permissions let the player manage the guild."""
    return bool(discord_api.session_permissions(request.session, discord_guild_id)
                & discord_api.MANAGE_GUILD)


def member_for(request, discord_guild_id: int,
               with_roles: bool = True) -> LocalMember:
    """Return the member the session knows, with the roles it holds there."""
    account = request.session.get(discord_api.USER_SESSION_KEY, {})
    permissions = discord_api.session_permissions(request.session, discord_guild_id)
    roles = roles_for(request, discord_guild_id) if with_roles else []
    return LocalMember(
        id=int(account.get('id') or 0),
        name=account.get('username', ''),
        roles=[LocalRole(role_id) for role_id in roles],
        guild_permissions=LocalPermissions(
            manage_guild=bool(permissions & discord_api.MANAGE_GUILD)),
    )


def roles_for(request, discord_guild_id: int) -> list[int]:
    """Return the role ids of the member, read once per guild per session."""
    stored = request.session.get(discord_api.ROLES_SESSION_KEY, {})
    key = str(discord_guild_id)
    if key in stored:
        return stored[key]
    account = request.session.get(discord_api.USER_SESSION_KEY, {})
    stored[key] = _read_roles(discord_guild_id, account.get('id'))
    request.session[discord_api.ROLES_SESSION_KEY] = stored
    return stored[key]


def _read_roles(discord_guild_id: int, discord_user_id) -> list[int]:
    """Return the role ids Discord reports, or none when it cannot be read."""
    if not discord_user_id:
        return []
    try:
        return discord_api.fetch_member_roles(discord_guild_id,
                                          int(discord_user_id))
    except Exception:
        logger.warning('Could not read the roles of %s in guild %s',
                       discord_user_id, discord_guild_id, exc_info=True)
        return []


def require_session_guild(request, discord_guild_id: int) -> dict:
    """Return the session's entry for a server the player belongs to."""
    guild = discord_api.session_guild(request.session, discord_guild_id)
    if guild is None:
        raise Http404(_('This server is not one of yours.'))
    return guild


def require_guild(discord_guild_id: int) -> Guild:
    """Return the guild the bot has a record of, refusing an unknown server."""
    guild = guild_by_discord_id(discord_guild_id)
    if guild is None:
        raise Http404(_('This server has no quiz record yet.'))
    return guild


def require_host_member(request, discord_guild_id: int) -> LocalMember:
    """Return the member, refusing one that may not host in the guild."""
    member = member_for(request, discord_guild_id)
    require_host(require_guild(discord_guild_id), member)
    return member


def require_admin_member(request, discord_guild_id: int) -> LocalMember:
    """Return the member, refusing one that may not manage the guild."""
    member = member_for(request, discord_guild_id, with_roles=False)
    require_admin(member)
    return member


class GuildAccessMixin(LoginRequiredMixin):
    """Refuse a request naming no server the logged in player belongs to.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        requested = kwargs.get('discord_guild_id')
        if requested is None:
            raise Http404(_('This server is not one of yours.'))
        guild_id = int(requested)
        require_session_guild(request, guild_id)
        self.check_guild_access(request, guild_id)
        return super().dispatch(request, *args, **kwargs)

    def check_guild_access(self, request, discord_guild_id: int) -> None:
        """Refuse the request when the player may not do this in the guild."""


class HostRequired(GuildAccessMixin):
    """Refuse a request that does not come from a host of the guild."""

    def check_guild_access(self, request, discord_guild_id: int) -> None:
        try:
            require_host_member(request, discord_guild_id)
        except PermissionError as error:
            raise PermissionDenied(str(error)) from error


class AdminRequired(GuildAccessMixin):
    """Refuse a request that does not come from an administrator of the guild."""

    def check_guild_access(self, request, discord_guild_id: int) -> None:
        try:
            require_admin_member(request, discord_guild_id)
        except PermissionError as error:
            raise PermissionDenied(str(error)) from error
