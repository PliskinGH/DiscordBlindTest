"""The forms the web admin submits, one per service operation."""

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Submit
from django import forms
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django_select2 import forms as select2_forms

from blindtest.constants import (GUESS_ANSWER_HINT, GUESS_ANSWER_LABEL,
                                 GUESS_SECONDARY_HINT, GUESS_SECONDARY_LABEL,
                                 MAX_CHOICES, MAX_YEAR)
from blindtest.models import Game, Question, QuizType, ScoringMode, Team
from blindtest.services.games import (copyable_games, create_game,
                                      game_option)
from blindtest.services.guessing import submit_guess
from blindtest.services.library import (add_question, edit_question, question_line,
                                        queueable_questions, split_answers)
from blindtest.services.teams import (copyable_team_option,
                                      copyable_team_queryset)

QUESTION_SEARCH_FIELDS = ['prompt__icontains',
                          'expected_answer__text__icontains',
                          'secondary_answer__text__icontains']
"""The lookups the pickers of questions search, as the Discord ones do."""

GAME_SEARCH_FIELDS = ['name__icontains']
"""The lookup the picker of games searches."""


class FullWidthWidgetMixin(object):
    """Stretch a picker to the width of the column it sits in."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs['style'] = 'width : 100%'


class SearchWidget(FullWidthWidgetMixin, select2_forms.Select2Widget):
    """A full width picker over the channels or roles Discord lists."""


class QuestionWidget(FullWidthWidgetMixin, select2_forms.ModelSelect2MultipleWidget):
    """A picker asking the server for the questions a game may still queue."""

    search_fields = QUESTION_SEARCH_FIELDS

    def label_from_instance(self, question: Question) -> str:
        """Return the question as the host picks it, answer included."""
        return question_line(question, with_answer=True)


TEAM_SEARCH_FIELDS = ['name__icontains', 'game__name__icontains']
"""The lookups the picker of teams searches, as the Discord one does."""


class TeamWidget(FullWidthWidgetMixin, select2_forms.ModelSelect2Widget):
    """A picker asking the server for the teams a host may copy."""

    search_fields = TEAM_SEARCH_FIELDS

    def label_from_instance(self, team: Team) -> str:
        """Return the team as the host picks it, with the game it played in."""
        return copyable_team_option(team)


class GameWidget(FullWidthWidgetMixin, select2_forms.ModelSelect2Widget):
    """A picker asking the server for the games a host may copy from."""

    search_fields = GAME_SEARCH_FIELDS

    def label_from_instance(self, game: Game) -> str:
        """Return the game as the host picks it, with its size and state."""
        return game_option(game, game.questions)


def form_errors(form: forms.Form) -> str:
    """Return every reason a form refused to be saved."""
    return ' '.join(error for errors in form.errors.values()
                    for error in errors)


def _choices(text: str) -> list[str]:
    """Return the options of a comma separated field."""
    return [part.strip() for part in text.split(',') if part.strip()]


class BootstrapForm(forms.Form):
    """A form rendered by crispy-forms in the Bootstrap layout the pages use.

    ``action`` is where the form posts to; without it a page would post to
    itself, which is a page and never a handler.

    A page renders several of these at once, so ``auto_id_prefix`` keeps their
    field ids apart: shared ids would leave a label and its widget pointing at
    whichever form came first.
    """

    submit_label = _('Save')
    auto_id_prefix = 'id'

    def __init__(self, *args, action: str = '', cancel_url: str = '',
                 **kwargs) -> None:
        kwargs.setdefault('auto_id', f'{self.auto_id_prefix}_%s')
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_action = action
        self.helper.form_tag = True
        self.helper.form_class = 'row g-3 align-items-end'
        self.helper.label_class = 'col-md-3 col-lg-2'
        self.helper.field_class = 'col-md-9 col-lg-4'
        self.helper.error_text_inline = True
        self.helper.include_media = False
        self.helper.add_input(
            Submit('save', self.submit_label,
                   css_id=f'{self.auto_id_prefix}_save'))
        if cancel_url:
            # The layout renders before the inputs, so Cancel sits left of Save.
            self.helper.layout.append(Div(
                HTML(format_html(
                    '<a class="btn btn-outline-secondary ms-2" href="{}">{}</a>',
                    cancel_url, _('Cancel'))),
                css_class='mb-3'))


def picker_choices(options: list[dict] | None,
                   current: int | None = None,
                   label_prefix: str = '#') -> list[tuple[str, str]]:
    """Return the ``(value, label)`` pairs of a picker, saved value included.

    A saved value Discord no longer lists is added back, so opening and saving
    a page again keeps it rather than quietly dropping it.
    """
    choices = [(str(option['id']), option['label']) for option in (options or [])]
    if current is not None:
        current = str(current)
        if all(current != given for given, _ in choices):
            choices.append((current, f'{label_prefix}{current}'))
    return choices


class _OptionsForm(BootstrapForm):
    """A form whose choices are the channels or roles Discord lists.

    The options are read per request rather than at import time, so the view
    that renders the page hands them in, as does ``current``: the value already
    saved, which the picker starts on.
    """

    label_prefix = '#'

    def __init__(self, *args, options: list[dict] | None = None,
                 current: int | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        choices = picker_choices(options, current, self.label_prefix)
        for field in self.fields.values():
            field.choices = choices
        if current is not None:
            self.initial[next(iter(self.fields))] = str(current)


def _option_field(label, placeholder, **kwargs):
    """Return a picker field reading the channels or roles Discord lists."""
    return forms.ChoiceField(
        label=label, widget=SearchWidget(
            attrs={'data-placeholder': placeholder}), **kwargs)


def _options_of(options: list[dict], limit: int = MAX_CHOICES) -> list[tuple[str, str]]:
    """Return the ``(pk, label)`` pairs of a picker fed by the domain."""
    return [(str(option['pk']), option['label'])
            for option in options[:limit]]


class PickerForm(_OptionsForm):
    """A form whose first field starts on the value already saved."""

    label_prefix = '#'


class QuestionFields(BootstrapForm):
    """The fields of a question, the prompt first, as this admin asks for them."""

    auto_id_prefix = 'question'

    prompt = forms.CharField(label=_('Prompt'), max_length=500, required=False)
    answer = forms.CharField(label=_('Answer'), max_length=200)
    artist = forms.CharField(label=_('Artist'), max_length=200, required=False)
    variants = forms.CharField(
        label=_('Variants'), required=False,
        help_text=_('Other texts that count as this answer, separated by '
                    'commas.'))
    choices = forms.CharField(
        label=_('Choices'), required=False,
        help_text=_('Two or more make the question a multiple choice one, '
                    'separated by commas.'))
    year = forms.IntegerField(label=_('Year'), required=False, min_value=1,
                              max_value=MAX_YEAR)
    album = forms.CharField(label=_('Album'), required=False)
    media = forms.URLField(label=_('Media'), required=False,
                           help_text=_('Link players listen to, e.g. YouTube.'))


class AddQuestionForm(QuestionFields):
    """A new question, with the texts its answer accepts."""

    def service_kwargs(self) -> dict:
        """Return what ``add_question`` is called with."""
        data = self.cleaned_data
        secondary, secondary_variants = split_answers(data['artist'])
        return {'expected_text': data['answer'],
                'expected_variants': _choices(data['variants']),
                'secondary_text': secondary,
                'secondary_variants': secondary_variants,
                'prompt': data['prompt'], 'choices': _choices(data['choices']),
                'year': data['year'], 'album': data['album'],
                'media_url': data['media']}


class EditQuestionForm(QuestionFields):
    """A question being changed: an empty field clears what it holds."""

    answer = forms.CharField(label=_('Answer'), max_length=200, required=False)

    def service_kwargs(self) -> dict:
        """Return what ``edit_question`` is called with."""
        data = self.cleaned_data
        return {'answer': data['answer'], 'artist': data['artist'],
                'prompt': data['prompt'], 'choices': data['choices'],
                'year': str(data['year'] or ''), 'album': data['album'],
                'media': data['media']}

    def variants(self) -> list[str]:
        """Return the texts the answer accepts, none of them when the box is empty."""
        return _choices(self.cleaned_data['variants'])


class HostRoleForm(PickerForm):
    """The grant of host rights to a role of the server."""

    submit_label = _('Give host rights')
    auto_id_prefix = 'host_role'
    label_prefix = '@'

    role_id = forms.ChoiceField(
        label=_('Role'), widget=SearchWidget(
            attrs={'data-placeholder': _('Search a role')}),
        help_text=_('Everybody holding the role may host games.'))


class HostUserForm(BootstrapForm):
    """The grant of host rights to one member, named or mentioned."""

    submit_label = _('Give host rights')
    auto_id_prefix = 'host_user'

    mention = forms.CharField(
        label=_('Member'), max_length=100,
        help_text=_('A user mention (<@123456789>), or the name of somebody '
                    'in this server.'))


class TeamForm(BootstrapForm):
    """A team to create, named and optionally with its first member."""

    submit_label = _('Add the team')
    auto_id_prefix = 'team'

    name = forms.CharField(label=_('Team name'), max_length=100)

    member = forms.CharField(
        label=_('First member'), max_length=100, required=False,
        help_text=_('A user mention (<@123456789>), or the name of '
                    'somebody in this server.'))


class CopyTeamForm(BootstrapForm):
    """A team copied over from a game this server played before.

    The target is the game of the page, so the only thing to pick is the team to
    copy: the games are searchable as well as the team names.
    """

    submit_label = _('Copy the team')
    auto_id_prefix = 'copy_team'

    source = forms.ModelChoiceField(
        label=_('Copy from'), queryset=Team.objects.none(),
        widget=TeamWidget(
            attrs={'data-placeholder': _('Search a team')}))

    def __init__(self, *args, game=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if game is not None:
            teams = copyable_team_queryset(game.guild, game)
            self.fields['source'].queryset = teams
            self.fields['source'].widget.queryset = teams
            self.fields['source'].widget.data_url = reverse(
                'webadmin:game_search',
                args=[game.guild.discord_id, game.pk])


class TeamMemberForm(BootstrapForm):
    """A member to add to a team, named or mentioned."""

    submit_label = _('Add the member')
    auto_id_prefix = 'team_member'

    member = forms.CharField(
        label=_('Member'), max_length=100,
        help_text=_('A user mention (<@123456789>), or the name of '
                    'somebody in this server.'))


class RenameTeamForm(BootstrapForm):
    """Another name for a team."""

    submit_label = _('Rename the team')
    auto_id_prefix = 'rename_team'

    name = forms.CharField(label=_('Team name'), max_length=100)


class ChannelForm(PickerForm):
    """The channel the games of a server are played in."""

    auto_id_prefix = 'channel'

    channel_id = forms.ChoiceField(
        label=_('Channel'), widget=SearchWidget(
            attrs={'data-placeholder': _('Search a channel')}))


class PingRoleForm(PickerForm):
    """The role a game pings when it opens."""

    auto_id_prefix = 'ping'
    label_prefix = '@'

    role_id = _option_field(_('Role'), _('Search a role'))


class SetupGameForm(BootstrapForm):
    """A game being set up: where it is played, how it is played and named."""

    submit_label = _('Set up a game')
    auto_id_prefix = 'setup'

    channel_id = _option_field(
        _('Channel'), _('Search a channel'), required=False,
        help_text=_('Left empty, the channel the server defaults to is used.'))
    ping_role_id = _option_field(
        _('Ping role'), _('Search a role'), required=False,
        help_text=_('Left empty, the role the server defaults to is used, '
                    'which may be none.'))
    name = forms.CharField(
        label=_('Name'), max_length=100, required=False,
        help_text=_('Left empty, the game is named after its type.'))
    quiz_type = forms.ChoiceField(
        label=_('Quiz type'), choices=QuizType.choices,
        initial=QuizType.BLIND_TEST,
        help_text=_('Decides how each round of the game is played.'))
    scoring_mode = forms.ChoiceField(
        label=_('Scoring mode'), choices=ScoringMode.choices,
        initial=ScoringMode.STANDARD,
        help_text=_('How the points of a round are shared out.'))

    def __init__(self, *args, channels: list[dict] | None = None,
                 roles: list[dict] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields['channel_id'].choices = picker_choices(channels)
        self.fields['ping_role_id'].choices = picker_choices(roles, None, '@')

    def service_kwargs(self) -> dict:
        """Return what ``create_game`` is called with."""
        data = self.cleaned_data
        return {'channel_id': _given(data['channel_id'], int),
                'ping_role_id': _given(data['ping_role_id'], int),
                'name': data['name'], 'quiz_type': data['quiz_type'],
                'scoring_mode': data['scoring_mode']}


def _given(text: str, cast):
    """Return the value of a picker field, None when it was left empty."""
    text = (text or '').strip()
    return cast(text) if text else None


class _QueueForm(BootstrapForm):
    """A form that may play the questions it queues as another quiz type."""

    quiz_type = forms.ChoiceField(
        label=_('Played as'), required=False,
        help_text=_('Queues them as this type rather than the one of the game.'))

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields['quiz_type'].choices = [
            ('', _('As the game is')), *QuizType.choices]


class QueueForm(_QueueForm):
    """Questions to add to the queue of a game."""

    submit_label = _('Queue the questions')
    auto_id_prefix = 'queue'

    questions = forms.ModelMultipleChoiceField(
        label=_('Questions'),
        queryset=Question.objects.none(),
        widget=QuestionWidget(
            attrs={'data-placeholder': _('Search a question')}),
        help_text=_('Questions the quiz type cannot play are skipped.'))

    def __init__(self, *args, game=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if game is not None:
            self.fields['questions'].queryset = queueable_questions(game)
            self.fields['questions'].widget.queryset = (
                self.fields['questions'].queryset)
            self._search_url(game)

    def _search_url(self, game) -> None:
        """Point the picker at the search of this game, in its server."""
        widget = self.fields['questions'].widget
        widget.data_url = reverse('webadmin:game_search',
                                  args=[game.guild.discord_id, game.pk])

    def pks(self) -> list[int]:
        """Return the questions to queue, as the service wants their pks."""
        return list(self.cleaned_data['questions'].values_list('pk', flat=True))


class UnqueueForm(BootstrapForm):
    """Queued rounds to remove from a game."""

    submit_label = _('Remove the questions')
    auto_id_prefix = 'unqueue'

    rounds = forms.MultipleChoiceField(
        label=_('Queued questions'), required=False,
        widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, options: list[dict] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields['rounds'].choices = _options_of(options or [])

    def pks(self) -> list[int]:
        """Return the queued rounds to remove, as the service wants."""
        return [int(pk) for pk in self.cleaned_data['rounds']]


class CopyForm(_QueueForm):
    """Questions copied over from a game this server played before."""

    submit_label = _('Copy the questions')
    auto_id_prefix = 'copy'

    source = forms.ModelChoiceField(
        label=_('Copy from'), queryset=Game.objects.none(),
        widget=GameWidget(
            attrs={'data-placeholder': _('Search a game')}))

    def __init__(self, *args, game=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if game is not None:
            games = copyable_games(game.guild, game)
            self.fields['source'].queryset = games
            self.fields['source'].widget.queryset = games
            self.fields['source'].widget.data_url = reverse(
                'webadmin:game_search', args=[game.guild.discord_id, game.pk])


class GuessForm(BootstrapForm):
    """A player's guess for the round in play."""

    submit_label = _('Guess')
    auto_id_prefix = 'guess'

    answer = forms.CharField(
        label=GUESS_ANSWER_LABEL, max_length=200, required=False,
        help_text=GUESS_ANSWER_HINT)
    secondary_answer = forms.CharField(
        label=GUESS_SECONDARY_LABEL, max_length=200, required=False,
        help_text=GUESS_SECONDARY_HINT)

    def service_kwargs(self) -> dict:
        """Return what ``submit_guess`` is called with."""
        data = self.cleaned_data
        return {'text': data['answer'],
                'secondary_text': data['secondary_answer']}


class ChoiceGuessForm(BootstrapForm):
    """A player's pick in a multiple choice round."""

    submit_label = _('Guess')
    auto_id_prefix = 'guess'

    choice = forms.ChoiceField(label=GUESS_ANSWER_LABEL)
    secondary_answer = forms.CharField(
        label=GUESS_SECONDARY_LABEL, max_length=200, required=False,
        help_text=GUESS_SECONDARY_HINT)

    def __init__(self, *args, options: list[dict] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields['choice'].choices = _options_of(options or [])
