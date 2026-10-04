"""The control room where a host runs a game from the browser."""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView
from django_select2.views import AutoResponseView

from blindtest import services
from blindtest.models import Game, QuizType

from .. import discord_api
from ..forms import (ChoiceGuessForm, CopyForm, GuessForm, QueueForm,
                     SetupGameForm, UnqueueForm, form_errors)
from ..permissions import (GuildAccessMixin, HostRequired, member_for,
                           require_guild, require_host)

CLEAR = 'clear'


def _back(game: Game):
    """Return the control room of a game the visitor is sent back to."""
    return redirect('webadmin:game', discord_guild_id=game.guild.discord_id,
                    game_pk=game.pk)


def _guild_id(kwargs) -> int:
    """Return the server the URL names."""
    return int(kwargs['discord_guild_id'])


def _game(request, kwargs) -> Game:
    """Return the game the URL names, refusing one of another server."""
    try:
        return services.game_by_pk(require_guild(_guild_id(kwargs)),
                                   int(kwargs['game_pk']))
    except ValueError as error:
        raise Http404(str(error)) from error


def _host_of(request, kwargs):
    """Return the member the session knows, refusing one that may not host."""
    return require_host(request, _guild_id(kwargs))


def _guess_form(round_, data=None):
    """Return the answer form of a round, of the kind its type is played with."""
    display = services.guess_form(round_)
    if display['type'] == QuizType.MULTIPLE_CHOICE:
        return ChoiceGuessForm(data, options=display['options'])
    return GuessForm(data)


def _hosts(request, game) -> bool:
    """Return True when the session may host in the server of the game.

    The roles come from the session, so a page polling every two seconds does
    not ask Discord who the player is on every poll.
    """
    guild = require_guild(game.guild.discord_id)
    return services.is_host(guild, member_for(request, guild.discord_id))


def _state(request, game, is_host: bool = False) -> dict:
    """Return what the live partial of a game shows, to one player.

    ``is_host`` decides whether the host controls are part of it, so the same
    partial serves the control room and the answer page.
    """
    state = services.control_state(game, request.user)
    return {**state, 'is_host': is_host}


def _guess_page(request, game, round_, data=None):
    """Return the answer page of a round, beside what the player answered."""
    return render(request, 'webadmin/guess.html', {
        'guild': game.guild, 'game': game,
        'state': _state(request, game, _hosts(request, game)),
        'form': _guess_form(round_, data)})


class GamesView(HostRequired, TemplateView):
    """List the games of this server and offer to set up the next one."""

    template_name = 'webadmin/games.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        guild_id = _guild_id(self.kwargs)
        guild = require_guild(guild_id)
        context.update(
            guild=guild, games=services.game_rows(guild),
            active=services.active_game(guild),
            setup_form=SetupGameForm(
                action=reverse('webadmin:game_setup', args=[guild_id]),
                channels=discord_api.fetch_bot_channels(guild_id),
                roles=discord_api.fetch_bot_roles(guild_id)))
        return context


class GameView(HostRequired, TemplateView):
    """The one page a host drives a game from: its queue, its rounds, its scores."""

    template_name = 'webadmin/game.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        guild_id = _guild_id(self.kwargs)
        game = _game(self.request, self.kwargs)
        args = [guild_id, game.pk]
        context.update(guild=game.guild, game=game,
                       state=_state(self.request, game, is_host=True),
                       queue_form=QueueForm(
                           action=reverse('webadmin:game_queue', args=args),
                           game=game),
                       unqueue_form=UnqueueForm(
                           action=reverse('webadmin:game_unqueue', args=args),
                           options=services.queued_choices(game)),
                       copy_form=CopyForm(
                           action=reverse('webadmin:game_copy', args=args),
                           game=game),
                       guess_url=reverse('webadmin:game_guess', args=args),
                       state_url=reverse('webadmin:game_state', args=args))
        return context


class GameStateView(HostRequired, TemplateView):
    """The part of the control room that follows the game while it plays.

    It is polled every couple of seconds, so it reads the game and nothing
    else: the queue pickers of the page it sits in are not rebuilt here.
    """

    template_name = 'webadmin/_game_state.html'

    def get_context_data(self, **kwargs):
        game = _game(self.request, self.kwargs)
        return {'guild': game.guild, 'game': game,
                'state': _state(self.request, game, is_host=True)}


class GameLiveView(GuildAccessMixin, TemplateView):
    """The part of a game that follows it while it plays, for a member.

    Every member of the server may read it: a host reveals a round from Discord
    and the players watching the answer page have to see it without reloading.
    """

    template_name = 'webadmin/_game_state.html'

    def get_context_data(self, **kwargs):
        game = _game(self.request, self.kwargs)
        return {'guild': game.guild, 'game': game,
                'state': _state(self.request, game,
                                _hosts(self.request, game))}


class SearchView(GuildAccessMixin, AutoResponseView):
    """Serve the options of a picker, to the members of a server.

    django-select2 reads the widget a page rendered out of the cache and asks it
    for the options matching a term. Naming the server in the URL keeps a member
    of one server from searching the pickers of another.
    """


class SetupGameView(HostRequired, View):
    """Set up a game of this server, without announcing it."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = _guild_id(kwargs)
        form = SetupGameForm(
            request.POST, channels=discord_api.fetch_bot_channels(guild_id),
            roles=discord_api.fetch_bot_roles(guild_id))
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return redirect('webadmin:games', discord_guild_id=guild_id)
        try:
            game = services.create_game(
                require_guild(guild_id),
                host_member=_host_of(request, kwargs),
                state=Game.State.SETUP, **form.service_kwargs())
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return redirect('webadmin:games', discord_guild_id=guild_id)
        return _back(game)


class PublishGameView(HostRequired, View):
    """Announce a prepared game, leaving the announcement to the worker."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        game = _game(request, kwargs)
        try:
            result, _broadcast = services.announce_game(
                game, _host_of(request, kwargs))
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(game)
        messages.success(request, f'{result["game_name"]} was announced.')
        return _back(game)


class QueueView(HostRequired, View):
    """Add questions to the queue of a game."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        game = _game(request, kwargs)
        member = _host_of(request, kwargs)
        form = QueueForm(request.POST, game=game)
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return _back(game)
        try:
            result = services.queue_questions(
                game, member, form.pks(), form.cleaned_data['quiz_type'])
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(game)
        skipped = f' {result["skipped"]} skipped.' if result['skipped'] else ''
        messages.success(request, f'{result["added"]} question(s) queued.{skipped}')
        return _back(game)


class UnqueueView(HostRequired, View):
    """Drop the queued questions a host picked, or the whole queue."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        game = _game(request, kwargs)
        member = _host_of(request, kwargs)
        try:
            if request.POST.get('action') == CLEAR:
                dropped = services.clear_queue(game, member)
            else:
                form = UnqueueForm(request.POST,
                                   options=services.queued_choices(game))
                if not form.is_valid():
                    messages.error(request, form_errors(form))
                    return _back(game)
                dropped = services.unqueue_questions(game, member, form.pks())
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(game)
        messages.success(request, f'{dropped} queued question(s) dropped.')
        return _back(game)

class CopyQuestionsView(HostRequired, View):
    """Queue the questions of a game this server played before."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        game = _game(request, kwargs)
        member = _host_of(request, kwargs)
        form = CopyForm(request.POST, game=game)
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return _back(game)
        try:
            result = services.copy_questions_by_pk(
                game, member, form.cleaned_data['source'].pk,
                form.cleaned_data['quiz_type'])
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(game)
        messages.success(request, f'{result["added"]} question(s) copied.')
        return _back(game)


class NextRoundView(HostRequired, View):
    """Open the next round, leaving its message to the worker."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        game = _game(request, kwargs)
        try:
            result, _broadcast = services.open_round(
                game, _host_of(request, kwargs))
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(game)
        messages.success(request, f'Round {result["index"]} opened: '
                                  f'{result["host_text"]}')
        return _back(game)


class RevealRoundView(HostRequired, View):
    """Reveal the round in play, leaving its posts to the worker."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        game = _game(request, kwargs)
        try:
            result, _broadcast = services.close_round(
                services.current_round(game), _host_of(request, kwargs))
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(game)
        messages.success(request, f'Round {result["index"]} revealed.')
        return _back(game)


class EndGameView(HostRequired, View):
    """End a game and publish its final scores."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        game = _game(request, kwargs)
        try:
            _result, _posts = services.close_game(game, _host_of(request, kwargs))
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(game)
        messages.success(request, f'{game.display_name} ended.')
        return _back(game)


class GuessView(GuildAccessMixin, View):
    """Answer the round in play. Any member of the server may, host or not."""

    def get(self, request, *args, **kwargs):
        return self._answer(request, kwargs)

    def post(self, request, *args, **kwargs):
        return self._answer(request, kwargs)

    def _answer(self, request, kwargs):
        """Show the round open to answer, recording an answer when one is posted."""
        game, round_ = self._round_in_play(request, kwargs)
        if round_ is None:
            # A member of the server may not read the control room, so the page
            # every one of them can open is where they are sent.
            messages.error(request, 'No round is open to answer right now.')
            return redirect('webadmin:guild',
                            discord_guild_id=game.guild.discord_id)
        data = request.POST if request.method == 'POST' else None
        if data is not None:
            try:
                self._submit(request, round_, data)
            except (PermissionError, ValueError) as error:
                messages.error(request, error)
            else:
                messages.success(request, 'Your answer is in.')
                data = None
        return _guess_page(request, game, round_, data)

    def _submit(self, request, round_, data) -> None:
        """Record the answer the form carries, as the round type expects it."""
        form = _guess_form(round_, data)
        if not form.is_valid():
            raise ValueError(form_errors(form))
        if isinstance(form, ChoiceGuessForm):
            services.submit_multiple_choice(
                round_, request.user, int(form.cleaned_data['choice']),
                form.cleaned_data['artist'])
            return
        services.submit_guess(round_, request.user, **form.service_kwargs())

    def _round_in_play(self, request, kwargs):
        """Return the game and the round open in it, if any."""
        game = _game(request, kwargs)
        round_ = services.current_round(game)
        return game, round_ if round_ is not None and round_.is_active else None
