"""The forms the web admin submits, one per service operation."""

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Submit
from django import forms
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django_select2 import forms as select2_forms

from blindtest.constants import MAX_YEAR
from blindtest.services import split_answers


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


class PickerForm(BootstrapForm):
    """A form whose choices are the channels or roles Discord lists.

    The options are read per request rather than at import time, so the view
    that renders the page hands them in, as does ``current``: the value already
    saved, which the picker starts on. A saved value Discord no longer lists is
    added back as an option, so opening and saving the page again keeps it
    rather than quietly dropping it.
    """

    label_prefix = '#'

    def __init__(self, *args, options: list[dict] | None = None,
                 current: int | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        choices = [(str(option['id']), option['label'])
                   for option in (options or [])]
        if current is not None:
            current = str(current)
            if all(current != given for given, _ in choices):
                choices.append((current, f'{self.label_prefix}{current}'))
            self.initial[next(iter(self.fields))] = current
        for field in self.fields.values():
            field.choices = choices


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
        """Return what ``services.add_question`` is called with."""
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
        """Return what ``services.edit_question`` is called with."""
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
        label=_('Role'), widget=select2_forms.Select2Widget(
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


class ChannelForm(PickerForm):
    """The channel the games of a server are played in."""

    auto_id_prefix = 'channel'

    channel_id = forms.ChoiceField(
        label=_('Channel'), widget=select2_forms.Select2Widget(
            attrs={'data-placeholder': _('Search a channel')}))


class PingRoleForm(PickerForm):
    """The role a game pings when it opens."""

    auto_id_prefix = 'ping'
    label_prefix = '@'

    role_id = forms.ChoiceField(
        label=_('Role'), widget=select2_forms.Select2Widget(
            attrs={'data-placeholder': _('Search a role')}))