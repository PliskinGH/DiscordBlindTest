"""The library a server's hosts fill with questions and their answers."""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView
from blindtest.services.library import (add_question, edit_question,
                                        editable_question, own_questions,
                                        question_line, remove_answer,
                                        remove_question, set_variants,
                                        unused_answers, unused_questions)


from ..forms import AddQuestionForm, EditQuestionForm, form_errors
from ..permissions import HostRequired, require_guild, require_host_member


def _values_of(question) -> dict:
    """Return the values the edit form shows for a question."""
    return {'answer': question.expected_answer.text,
            'artist': (question.secondary_answer.text
                       if question.secondary_answer else ''),
            'variants': ', '.join(
                variant.text
                for variant in question.expected_answer.variants.all()),
            'prompt': question.prompt,
            'choices': ', '.join(choice.text
                                 for choice in question.choices.all()),
            'year': question.year or '', 'album': question.album,
            'media': question.media_url}


def _back(guild_id: int):
    """Return the library page the visitor is sent back to."""
    return redirect('webadmin:library', discord_guild_id=guild_id)


class LibraryView(HostRequired, TemplateView):
    """Show the questions of this server's library, and the unused ones."""

    template_name = 'webadmin/library.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        guild_id = int(self.kwargs['discord_guild_id'])
        guild = require_guild(guild_id)
        wanted = self.request.GET.get('q', '')
        context.update(guild=guild, wanted=wanted,
                       questions=own_questions(guild, wanted),
                       unused_questions=unused_questions(guild),
                       unused_answers=unused_answers(guild),
                       question_form=AddQuestionForm(
                           action=reverse('webadmin:question_add',
                                          args=[guild_id])))
        return context


class AddQuestionView(HostRequired, View):
    """Create a question in this server's own library."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        form = AddQuestionForm(request.POST)
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return _back(guild_id)
        try:
            result = add_question(
                require_guild(guild_id), require_host_member(request, guild_id),
                **form.service_kwargs())
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(guild_id)
        messages.success(request, f'Question saved: {result["label"]}.')
        return _back(guild_id)


class EditQuestionView(HostRequired, View):
    """Change the fields of one question of this server's library."""

    http_method_names = ['get', 'post', 'options']

    def get(self, request, *args, **kwargs):
        return self._page(request, int(kwargs['discord_guild_id']),
                          int(kwargs['question_pk']))

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        question_pk = int(kwargs['question_pk'])
        form = EditQuestionForm(request.POST)
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return self._page(request, guild_id, question_pk)
        try:
            guild = require_guild(guild_id)
            member = require_host_member(request, guild_id)
            edit_question(guild, member, question_pk,
                                   **form.service_kwargs())
            question = editable_question(guild, member, question_pk)
            # The variants ride along the answer, renamed or not.
            set_variants(guild, member, question.expected_answer.text,
                                  form.variants())
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return self._page(request, guild_id, question_pk)
        label = question_line(question, with_answer=True)
        messages.success(request, f'Question updated: {label}.')
        return _back(guild_id)

    def _page(self, request, guild_id: int, question_pk: int):
        """Return the edit form of one question, or 404 when it is not ours."""
        guild = require_guild(guild_id)
        try:
            question = editable_question(
                guild, require_host_member(request, guild_id), question_pk)
        except (PermissionError, ValueError) as error:
            raise Http404(str(error)) from error
        return render(request, 'webadmin/question.html', {
            'guild': guild, 'question': question,
            'form': EditQuestionForm(
                initial=_values_of(question),
                action=request.get_full_path(),
                cancel_url=reverse('webadmin:library',
                                  args=[guild_id]))})


class RemoveQuestionView(HostRequired, View):
    """Remove a question of this server's library that no game has played."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        try:
            remove_question(require_guild(guild_id),
                                      require_host_member(request, guild_id),
                                      int(kwargs['question_pk']))
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(guild_id)
        messages.success(request, 'Question removed.')
        return _back(guild_id)


class RemoveAnswerView(HostRequired, View):
    """Remove an answer of this server's library that no question uses."""

    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        guild_id = int(kwargs['discord_guild_id'])
        try:
            answer = remove_answer(require_guild(guild_id),
                                            require_host_member(request, guild_id),
                                            int(kwargs['answer_pk']))
        except (PermissionError, ValueError) as error:
            messages.error(request, error)
            return _back(guild_id)
        messages.success(request, f'Answer removed: {answer.text}.')
        return _back(guild_id)
