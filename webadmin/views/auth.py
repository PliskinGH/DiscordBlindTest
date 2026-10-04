"""The Discord login the web admin guards its pages with."""

import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from discordcore.models import Player

from .. import discord_api


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