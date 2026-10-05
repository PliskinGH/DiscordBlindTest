"""The settings a server's administrators manage."""

import requests
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView


from discordcore.mentions import parse_mention
from blindtest.services.guilds import (add_host, clear_default_channel,
                                       clear_default_ping_role,
                                       default_channel_of,
                                       default_ping_role_of, hosts_of,
                                       remove_host, set_default_channel,
                                       set_default_ping_role)

from .. import discord_api
from ..forms import (ChannelForm, HostRoleForm, HostUserForm, PingRoleForm,
                     form_errors)
from ..permissions import AdminRequired, require_admin_member, require_guild

CLEAR = 'clear'
REMOVE = 'remove'


def _back(guild_id: int):
    """Return the settings page the visitor is sent back to."""
    return redirect('webadmin:settings', discord_guild_id=guild_id)


def _apply(request, operation, *args) -> bool:
    """Run a settings operation, reporting why it refused; say if it did."""
    try:
        operation(*args)
    except (PermissionError, ValueError) as error:
        messages.error(request, error)
        return False
    return True


def _mentioned_member(request, guild_id: int, name: str) -> str | None:
    """Return the mention of the member named, telling the player when none is.

    A mention is taken as it stands; anything else is looked up in the server,
    which is what makes typing a name worth doing. Either way the Discord name
    of a member is kept, so the settings page need not ask Discord for it again.
    """
    given = name.strip()
    try:
        if given.startswith('<@'):
            return _remember(guild_id, given)
        member = discord_api.fetch_bot_member(guild_id, given)
    except requests.RequestException:
        messages.error(request, _('Discord could not be reached, try again.'))
        return None
    if member is None:
        messages.error(request, _('No member named "%(name)s" in this server. '
                                  'Paste a user mention instead.')
                       % {'name': given})
        return None
    discord_api.remember_member(member['id'], member.get('name') or '')
    return member['mention']


def _remember(guild_id: int, mention: str) -> str:
    """Read the name behind a pasted user mention once, keeping it.

    A role mention names no member to remember, a mention too malformed to read
    is left for the service to refuse, and a Discord that cannot be reached is no
    reason to withhold the host rights the administrator is granting.
    """
    try:
        is_role, discord_id = parse_mention(mention)
    except ValueError:
        return mention
    if not is_role:
        discord_api.member_label(guild_id, discord_id)
    return mention


class SettingsView(AdminRequired, TemplateView):
    """Show where the games of this server are played, pinged and hosted."""

    template_name = 'webadmin/settings.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        guild_id = int(self.kwargs['discord_guild_id'])
        guild = require_guild(guild_id)
        channels = discord_api.fetch_bot_channels(guild_id)
        roles = discord_api.fetch_bot_roles(guild_id)
        channel_id = default_channel_of(guild)
        ping_role_id = default_ping_role_of(guild)
        context.update(
            guild=guild,
            channel=discord_api.channel_label(guild_id, channel_id),
            ping_role=discord_api.role_label(guild_id, ping_role_id),
            hosts=discord_api.host_labels(
                guild_id, hosts_of(guild)),
            channel_form=ChannelForm(
                options=channels, current=channel_id,
                action=reverse('webadmin:setting_channel',
                               args=[guild_id])),
            ping_form=PingRoleForm(
                options=roles, current=ping_role_id,
                action=reverse('webadmin:setting_ping', args=[guild_id])),
            host_role_form=HostRoleForm(
                options=roles,
                action=reverse('webadmin:setting_hosts', args=[guild_id])),
            host_user_form=HostUserForm(
                action=reverse('webadmin:setting_hosts', args=[guild_id])))
        return context


class ChannelView(AdminRequired, View):
    """Set or clear the channel the games of this server are played in."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        guild = require_guild(guild_id)
        member = require_admin_member(request, guild_id)
        if request.POST.get('action') == CLEAR:
            if _apply(request, clear_default_channel, guild, member):
                messages.success(request, 'The games follow the host again.')
            return _back(guild_id)
        form = ChannelForm(
            request.POST, options=discord_api.fetch_bot_channels(guild_id),
            current=default_channel_of(guild))
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return _back(guild_id)
        channel_id = int(form.cleaned_data['channel_id'])
        if _apply(request, set_default_channel, guild, channel_id,
                  member):
            label = discord_api.channel_label(guild_id, channel_id)
            messages.success(request, f'Games are now played in {label}.')
        return _back(guild_id)


class PingRoleView(AdminRequired, View):
    """Set or clear the role a game pings when it opens."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        guild = require_guild(guild_id)
        member = require_admin_member(request, guild_id)
        if request.POST.get('action') == CLEAR:
            if _apply(request, clear_default_ping_role, guild, member):
                messages.success(request, 'The games ping nobody by default.')
            return _back(guild_id)
        form = PingRoleForm(
            request.POST, options=discord_api.fetch_bot_roles(guild_id),
            current=default_ping_role_of(guild))
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return _back(guild_id)
        role_id = int(form.cleaned_data['role_id'])
        if _apply(request, set_default_ping_role, guild, role_id, member):
            label = discord_api.role_label(guild_id, role_id)
            messages.success(request, f'The games now ping {label}.')
        return _back(guild_id)


class HostsView(AdminRequired, View):
    """Allow a role or a member to host, or withdraw that right."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        guild = require_guild(guild_id)
        member = require_admin_member(request, guild_id)
        removed = request.POST.get('action') == REMOVE
        if removed:
            mention = request.POST.get('mention', '')
        else:
            mention = self._mention_of(request, guild_id)
            if mention is None:
                return _back(guild_id)
        operation = remove_host if removed else add_host
        if _apply(request, operation, guild, mention, member):
            messages.success(request, f'{mention} can{"not " if removed else ""} '
                                      f'host here.')
        return _back(guild_id)

    def _mention_of(self, request, guild_id: int) -> str | None:
        """Return the mention the submitted host form asks for."""
        if 'role_id' in request.POST:
            form = HostRoleForm(
                request.POST, options=discord_api.fetch_bot_roles(guild_id))
            if not form.is_valid():
                messages.error(request, form_errors(form))
                return None
            return f'<@&{form.cleaned_data["role_id"]}>'
        form = HostUserForm(request.POST)
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return None
        return _mentioned_member(request, guild_id,
                                 form.cleaned_data['mention'])
