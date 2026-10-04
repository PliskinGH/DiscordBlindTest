"""The page of one server, and the way to add it to the bot."""

from django.contrib import messages
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from blindtest import services
from discordcore.members import LocalGuild, can_manage_guild

from ..permissions import (GuildAccessMixin, member_for, require_guild,
                           require_session_guild)


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