"""The landing page: the servers this player shares with the bot."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from blindtest.services.games import active_game
from blindtest.services.guilds import guilds_by_discord_ids


from .. import discord_api
from ..permissions import can_manage


def _by_name(rows: list[dict]) -> list[dict]:
    """Return the rows ordered by server name, case-insensitively."""
    return sorted(rows, key=lambda row: row['name'].casefold())


class DashboardView(LoginRequiredMixin, TemplateView):
    """List the servers the bot knows, and offer the ones still to add."""

    template_name = 'webadmin/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        guilds = discord_api.session_guilds(self.request.session)
        rows = guilds_by_discord_ids([guild['id'] for guild in guilds])
        # None when Discord could not be read: nothing is then known missing.
        bot_guilds = discord_api.fetch_bot_guild_ids()
        present = bot_guilds if bot_guilds is not None else set()
        managed, others = [], []
        for guild in guilds:
            manage = can_manage(self.request, guild['id'])
            row = rows.get(guild['id'])
            if row is not None:
                left = bot_guilds is not None and guild['id'] not in bot_guilds
                managed.append({'discord_id': guild['id'], 'name': guild['name'],
                                'manage': manage,
                                'active_game': active_game(row),
                                'bot_left': left,
                                'invite_url': (discord_api.invite_url(guild['id'])
                                               if left else '')})
            elif manage:
                others.append({
                    'discord_id': guild['id'], 'name': guild['name'],
                    'invite_url': (discord_api.invite_url(guild['id'])
                                   if guild['id'] not in present else '')})
        context['guilds'] = _by_name(managed)
        context['others'] = _by_name(others)
        return context
