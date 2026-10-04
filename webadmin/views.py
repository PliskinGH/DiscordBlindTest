"""Discord login and the landing pages of the web admin."""

import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from blindtest import services
from discordcore.members import LocalGuild, can_manage_guild
from discordcore.models import Player

from . import discord_api
from .permissions import (GuildAccessMixin, can_manage, member_for, require_guild,
                         require_session_guild)


class LoginView(TemplateView):
    """Offer the Discord login button."""

    template_name = 'webadmin/login.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['discord_enabled'] = discord_api.discord_oauth_configured()
        return context


class DiscordConnectView(View):
    """Send the visitor to Discord to authorize the application."""

    http_method_names = ['get', 'options']

    def get(self, request, *args, **kwargs):
        if not discord_api.discord_oauth_configured():
            raise Http404(discord_api.DISCORD_OAUTH_DISABLED)
        state = secrets.token_urlsafe(32)
        request.session[discord_api.STATE_SESSION_KEY] = state
        params = urlencode({
            'client_id': settings.DISCORD_CLIENT_ID,
            'redirect_uri': discord_api.discord_redirect_uri(request),
            'response_type': 'code',
            'scope': discord_api.DISCORD_OAUTH_SCOPES,
            'state': state,
        })
        return HttpResponseRedirect(
            f'{discord_api.DISCORD_AUTHORIZE_URL}?{params}')


class DiscordCallbackView(View):
    """Finish the Discord flow: verify it, then log the player in."""

    http_method_names = ['get', 'options']

    def get(self, request, *args, **kwargs):
        if not discord_api.discord_oauth_configured():
            raise Http404(discord_api.DISCORD_OAUTH_DISABLED)
        state = request.session.pop(discord_api.STATE_SESSION_KEY, None)
        if request.GET.get('error'):
            return self._refuse(request, _('Discord login was cancelled.'))
        code = request.GET.get('code')
        if (not code or not state or not secrets.compare_digest(
                request.GET.get('state', ''), state)):
            return self._refuse(request, discord_api.DISCORD_OAUTH_ERROR)
        try:
            identity = discord_api.fetch_discord_identity(request, code)
        except (requests.RequestException, KeyError, ValueError):
            return self._refuse(request, discord_api.DISCORD_OAUTH_ERROR)
        player = Player.objects.from_discord(discord_api.account_of(identity))
        discord_api.store_identity(request.session, identity)
        login(request, player,
              backend='django.contrib.auth.backends.ModelBackend')
        return redirect('webadmin:dashboard')

    def _refuse(self, request, reason):
        """Send the visitor back to the login page with a message."""
        messages.error(request, reason)
        return redirect('webadmin:login')


def _by_name(rows: list[dict]) -> list[dict]:
    """Return the rows ordered by server name, case-insensitively."""
    return sorted(rows, key=lambda row: row['name'].casefold())


class DashboardView(LoginRequiredMixin, TemplateView):
    """List the servers the bot knows, and offer the ones still to add."""

    template_name = 'webadmin/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        guilds = discord_api.session_guilds(self.request.session)
        rows = services.guilds_by_discord_ids([guild['id'] for guild in guilds])
        bot_guilds = discord_api.fetch_bot_guild_ids()
        managed, others = [], []
        for guild in guilds:
            manage = can_manage(self.request, guild['id'])
            row = rows.get(guild['id'])
            if row is not None:
                managed.append({'discord_id': guild['id'], 'name': guild['name'],
                                'manage': manage,
                                'active_game': services.active_game(row)})
            elif manage:
                others.append({
                    'discord_id': guild['id'], 'name': guild['name'],
                    'invite_url': (discord_api.invite_url(guild['id'])
                                   if guild['id'] not in bot_guilds else '')})
        context['guilds'] = _by_name(managed)
        context['others'] = _by_name(others)
        return context


class AddGuildView(GuildAccessMixin, View):
    """Register one of the player's servers with the bot."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        guild = require_session_guild(request, guild_id)
        try:
            services.add_guild(
                LocalGuild(id=guild_id, name=guild['name']),
                member_for(request, guild_id, with_roles=False))
        except PermissionError as error:
            messages.error(request, error)
            return redirect('webadmin:dashboard')
        return redirect('webadmin:guild', discord_guild_id=guild_id)


class GuildView(GuildAccessMixin, TemplateView):
    """Show one server, the rights of the player in it and its running game."""

    template_name = 'webadmin/guild.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        guild_id = int(self.kwargs['discord_guild_id'])
        guild = require_guild(guild_id)
        member = member_for(self.request, guild_id)
        context.update(guild=guild,
                       is_admin=can_manage_guild(member),
                       is_host=services.is_host(guild, member),
                       active_game=services.active_game(guild))
        return context
