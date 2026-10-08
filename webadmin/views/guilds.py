"""The page of one server, and the way to add it to the bot."""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from blindtest.models import Broadcast
from discordcore.members import LocalGuild, can_manage_guild

from blindtest.services.broadcasts import (retry_broadcast,
                                            unfinished_broadcasts)
from blindtest.services.games import played_game
from blindtest.services.guilds import add_guild, is_host

from .. import discord_api
from ..permissions import (GuildAccessMixin, HostRequired, bot_left,
                           member_for, require_guild, require_session_guild)


class AddGuildView(GuildAccessMixin, View):
    """Register one of the player's servers with the bot."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        guild = require_session_guild(request, guild_id)
        try:
            add_guild(
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
        host = is_host(guild, member)
        gone = bot_left(self.request, guild_id)
        context.update(guild=guild,
                       is_admin=can_manage_guild(member),
                       is_host=host,
                       bot_left=gone,
                       invite_url=(discord_api.invite_url(guild_id) if gone else ''),
                       active_game=played_game(guild),
                       broadcasts=(unfinished_broadcasts(guild)
                                   if host else []))
        return context


class RetryBroadcastView(HostRequired, View):
    """Put a post that never went out back in line, so the bot tries it again."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        guild = require_guild(guild_id)
        # A post is retried on the page of the server it was meant for, so a
        # broadcast of another server is not this one.
        try:
            retry_broadcast(int(kwargs['broadcast_pk']), guild.pk)
        except Broadcast.DoesNotExist:
            messages.error(request, 'That post is gone.')
        else:
            messages.success(request, 'The post is queued again.')
        return redirect(_back(request, guild_id))


def _back(request, guild_id: int) -> str:
    """Return the page the retry was made from, so a host stays where it is."""
    target = request.POST.get('next', '')
    if url_has_allowed_host_and_scheme(target, allowed_hosts=None):
        return target
    return reverse('webadmin:guild', kwargs={'discord_guild_id': guild_id})
