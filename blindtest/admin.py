from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import Answer, AnswerVariant, Game, Guess, Question, Round, Team


class AnswerVariantInline(admin.TabularInline):
    model = AnswerVariant
    extra = 1
    fields = ('text',)


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ('id', 'text', 'guild', 'created_at')
    list_filter = ('guild',)
    search_fields = ('text', 'guild__name')
    autocomplete_fields = ('guild',)
    inlines = (AnswerVariantInline,)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ('id', 'guild', 'prompt', 'expected_answer', 'secondary_answer', 'year')
    list_filter = ('guild', 'year')
    search_fields = ('prompt', 'guild__name', 'expected_answer__text', 'secondary_answer__text', 'album')
    autocomplete_fields = ('guild', 'expected_answer', 'secondary_answer')
    filter_horizontal = ('choices',)


class RoundInline(admin.TabularInline):
    model = Round
    extra = 0
    fields = ('index', 'question', 'scoring_mode', 'started_at', 'revealed_at')
    readonly_fields = ('started_at', 'revealed_at')
    show_change_link = True


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ('id', 'display_name', 'host', 'type', 'state', 'scoring_mode',
                    'guild', 'channel_id', 'created_at', 'finished_at')
    list_filter = ('state', 'type', 'scoring_mode')
    search_fields = ('name', 'host__username', 'host__discord_name', 'guild__name',
                     'channel_id')
    date_hierarchy = 'created_at'
    inlines = (RoundInline,)
    autocomplete_fields = ('guild', 'host')
    fieldsets = (
        (None, {'fields': ('guild', 'channel_id', 'host', 'name', 'type', 'state',
                           'scoring_mode')}),
        (_('Dates'), {'fields': ('created_at', 'finished_at')}),
    )
    readonly_fields = ('created_at',)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'game', 'created_at')
    list_filter = ('game__state',)
    search_fields = ('name', 'game__host__username', 'game__host__discord_name')
    autocomplete_fields = ('game',)
    filter_horizontal = ('players',)
    readonly_fields = ('created_at',)


class GuessInline(admin.TabularInline):
    model = Guess
    extra = 0
    fields = ('player', 'team', 'text', 'secondary_text', 'text_correct',
              'secondary_correct', 'submitted_at')
    readonly_fields = ('submitted_at',)


@admin.register(Round)
class RoundAdmin(admin.ModelAdmin):
    list_display = ('id', 'game', 'index', 'question', 'type', 'scoring_mode',
                    'started_at', 'revealed_at')
    list_filter = ('game__state', 'type', 'scoring_mode')
    search_fields = ('question__prompt', 'question__expected_answer__text')
    autocomplete_fields = ('question',)
    inlines = (GuessInline,)


@admin.register(Guess)
class GuessAdmin(admin.ModelAdmin):
    list_display = ('id', 'round', 'player', 'team', 'text', 'secondary_text',
                    'text_correct', 'secondary_correct', 'submitted_at')
    list_filter = ('text_correct', 'secondary_correct')
    search_fields = ('text', 'secondary_text', 'player__username',
                     'player__discord_name', 'team__name')
    autocomplete_fields = ('round', 'player', 'team')


