from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, UniqueConstraint, functions
from django.utils.translation import gettext_lazy as _
from enum import property as enum_property

from .constants import DEFAULT_BLIND_TEST_PROMPT

class TextChoices(models.TextChoices):
    """A TextChoices that exposes its capitalized
       label as a property of the value."""

    @enum_property
    def display_name(self):
        return self._label_.capitalize()

class ScoringMode(TextChoices):
    STANDARD = 'STANDARD', _('standard (fixed points)')
    FIRST_ONLY = 'FIRST_ONLY', _('first correct only')
    SPEED = 'SPEED', _('speed / time bonus')


class QuizType(TextChoices):
    """How rounds are played: the game sets the type, a round may override it."""

    BLIND_TEST = 'BLIND_TEST', _('blind test')
    OPEN = 'OPEN', _('open question')
    MULTIPLE_CHOICE = 'MULTIPLE_CHOICE', _('multiple choice')


class Answer(models.Model):
    """An answer text or multiple choice option."""

    text = models.CharField(_('text'), max_length=200)
    guild = models.ForeignKey(
        'discordcore.Guild', on_delete=models.CASCADE, null=True, blank=True,
        related_name='answers', verbose_name=_('guild'),
        help_text=_('Leave empty for the global library.')
    )
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('answer')
        verbose_name_plural = _('answers')
        ordering = ('text',)
        constraints = [
            UniqueConstraint(functions.Lower('text'), 'guild',
                             name='unique_answer_text', nulls_distinct=False,
                             violation_error_message=_('This answer already exists.')),
        ]

    def __str__(self) -> str:
        return self.text


class AnswerVariant(models.Model):
    """Alternative acceptable spelling, synonym or alias for an answer."""

    answer = models.ForeignKey(Answer, on_delete=models.CASCADE,
                               related_name='variants', verbose_name=_('answer'))
    text = models.CharField(_('variant text'), max_length=200)
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('answer variant')
        verbose_name_plural = _('answer variants')
        ordering = ('answer', 'text')
        constraints = [
            UniqueConstraint('answer', functions.Lower('text'),
                             name='unique_answer_variant',
                             violation_error_message=_(
                                 'This variant is already registered for this answer.')),
        ]

    def __str__(self) -> str:
        return f'{self.text} -> {self.answer.text}'


class Question(models.Model):
    """A quiz question, playable by any of the round types."""

    guild = models.ForeignKey(
        'discordcore.Guild', on_delete=models.CASCADE, null=True, blank=True,
        related_name='questions', verbose_name=_('guild'),
        help_text=_('Leave empty for the global library.')
    )
    prompt = models.CharField(_('prompt'), max_length=500, blank=True)
    expected_answer = models.ForeignKey(
        Answer, on_delete=models.PROTECT, related_name='expected_for_questions',
        verbose_name=_('expected answer')
    )
    secondary_answer = models.ForeignKey(
        Answer, on_delete=models.PROTECT, null=True, blank=True,
        related_name='secondary_for_questions', verbose_name=_('secondary answer'),
        help_text=_('Artist for blind tests, or secondary required answer.')
    )
    choices = models.ManyToManyField(
        Answer, blank=True, related_name='choice_for_questions',
        verbose_name=_('choices'), help_text=_('Options for multiple choice questions.')
    )
    media_url = models.URLField(_('media URL'), blank=True)
    album = models.CharField(_('album'), max_length=200, blank=True)
    year = models.PositiveSmallIntegerField(_('year'), blank=True, null=True)
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('question')
        verbose_name_plural = _('questions')
        ordering = ('-created_at',)

    def __str__(self) -> str:
        """Identify the question; the answers are never implicit."""
        return self.prompt or _('Question #%s') % self.pk

    @property
    def effective_prompt(self) -> str:
        """Return the prompt to show, defaulting for a blind test round."""
        return self.prompt or DEFAULT_BLIND_TEST_PROMPT

    @property
    def answer_text(self) -> str:
        """Return the answers of the question, without its prompt."""
        if self.secondary_answer_id:
            return f'{self.expected_answer.text} ({self.secondary_answer.text})'
        return self.expected_answer.text


def question_problem(question: Question, quiz_type: str) -> str | None:
    """Return why a quiz type cannot play a question, or None when it can."""
    if quiz_type == QuizType.BLIND_TEST:
        return None
    if not question.prompt.strip():
        return _('This question needs a prompt.')
    if quiz_type == QuizType.MULTIPLE_CHOICE:
        choices = list(question.choices.all())
        if len(choices) < 2:
            return _('A multiple choice question needs at least two choices.')
        if question.expected_answer_id not in {choice.pk for choice in choices}:
            return _('The expected answer must be one of the choices.')
    return None


class Game(models.Model):
    """One quiz session, hosted by a player in a Discord channel."""

    class State(TextChoices):
        SETUP = 'SETUP', _('being prepared')
        RUNNING = 'RUNNING', _('running')
        PAUSED = 'PAUSED', _('paused')
        FINISHED = 'FINISHED', _('finished')

    guild = models.ForeignKey('discordcore.Guild', on_delete=models.PROTECT,
                              related_name='games', verbose_name=_('guild'))
    channel_id = models.BigIntegerField(_('Discord channel ID'))
    ping_role_id = models.BigIntegerField(
        _('ping role ID'), blank=True, null=True,
        help_text=_('The role pinged for the game when ' \
                    'it is published and each round opens.'))
    name = models.CharField(_('name'), max_length=100, blank=True)
    type = models.CharField(_('type'), max_length=20, choices=QuizType,
                            default=QuizType.BLIND_TEST)
    host = models.ForeignKey('discordcore.Player', on_delete=models.PROTECT,
                             related_name='hosted_games', verbose_name=_('host'))
    state = models.CharField(_('state'), max_length=20,
                             choices=State, default=State.RUNNING)
    scoring_mode = models.CharField(
        _('scoring mode'), max_length=20, choices=ScoringMode, default=ScoringMode.STANDARD
    )
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)
    finished_at = models.DateTimeField(_('finished at'), blank=True, null=True)

    class Meta:
        verbose_name = _('game')
        verbose_name_plural = _('games')
        ordering = ('-created_at',)
        constraints = [
            UniqueConstraint('guild', condition=~Q(state='FINISHED'),
                             name='unique_active_game_per_guild',
                             violation_error_message=_(
                                 'A game is already running in this server.')),
        ]

    def __str__(self) -> str:
        return f'{self.host} - {self.State(self.state).display_name} (#{self.pk})'

    @property
    def is_running(self) -> bool:
        return self.state in (self.State.RUNNING, self.State.PAUSED)

    @property
    def is_preparing(self) -> bool:
        """Return True while the game is prepared but not published yet."""
        return self.state == self.State.SETUP

    @property
    def is_active(self) -> bool:
        """Return True while the game can still take queued questions."""
        return self.state != self.State.FINISHED

    @property
    def display_name(self) -> str:
        """Return the name of the game, defaulting to '<type> #<number>'."""
        return self.name or f'{QuizType(self.type).display_name} #{self.pk}'


class Team(models.Model):
    """A team answering together during one game."""

    game = models.ForeignKey(Game, on_delete=models.CASCADE,
                             related_name='teams', verbose_name=_('game'))
    name = models.CharField(_('name'), max_length=100)
    players = models.ManyToManyField('discordcore.Player', related_name='teams',
                                     blank=True, verbose_name=_('players'))
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('team')
        verbose_name_plural = _('teams')
        ordering = ('game', 'name')
        constraints = [
            UniqueConstraint('game', 'name', name='unique_team_name_per_game',
                             violation_error_message=_(
                                 'This team name is already used in this game.')),
        ]

    def __str__(self) -> str:
        return f'{self.name} ({self.game})'


class Round(models.Model):
    """One question played during a game, with the answers it collected."""

    game = models.ForeignKey(Game, on_delete=models.CASCADE,
                             related_name='rounds', verbose_name=_('game'))
    index = models.PositiveSmallIntegerField(_('number'))
    question = models.ForeignKey(Question, on_delete=models.PROTECT,
                                 related_name='rounds', verbose_name=_('question'))
    type = models.CharField(
        _('type'), max_length=20, choices=QuizType, blank=True, default='',
        help_text=_('Inherits from the game type if left empty.'))
    scoring_mode = models.CharField(
        _('scoring mode'), max_length=20, choices=ScoringMode, blank=True, default='',
        help_text=_('Inherits from the game scoring mode if left empty.')
    )
    started_at = models.DateTimeField(_('started at'), blank=True, null=True, default=None)
    revealed_at = models.DateTimeField(_('revealed at'), blank=True, null=True, default=None)

    class Meta:
        verbose_name = _('round')
        verbose_name_plural = _('rounds')
        ordering = ('game', 'index')
        constraints = [
            UniqueConstraint('game', 'index', name='unique_round_number',
                             violation_error_message=_(
                                 'This round number is already used in this game.')),
        ]

    def __str__(self) -> str:
        return f'{self.game} - round {self.index}'

    @property
    def effective_scoring_mode(self) -> str:
        return self.scoring_mode or self.game.scoring_mode

    @property
    def effective_type(self) -> str:
        """Return the quiz type of the round, inherited from its game."""
        return self.type or self.game.type

    def clean(self) -> None:
        """Refuse a question the type of the round cannot play."""
        super().clean()
        if self.question_id is None:
            return
        problem = question_problem(self.question, self.effective_type)
        if problem:
            raise ValidationError({'question': problem})

    @property
    def is_started(self) -> bool:
        return self.started_at is not None

    @property
    def is_revealed(self) -> bool:
        return self.revealed_at is not None

    @property
    def is_active(self) -> bool:
        return self.is_started and not self.is_revealed


class Guess(models.Model):
    """A player's answer for one round: the first answer is the final one."""

    round = models.ForeignKey(Round, on_delete=models.CASCADE,
                              related_name='guesses', verbose_name=_('round'))
    player = models.ForeignKey('discordcore.Player', on_delete=models.SET_NULL,
                               null=True, blank=True,
                               related_name='guesses', verbose_name=_('player'))
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True,
                             related_name='guesses', verbose_name=_('team'))
    text = models.CharField(_('guessed text'), max_length=200, blank=True)
    secondary_text = models.CharField(_('secondary text'), max_length=200, blank=True)
    text_correct = models.BooleanField(_('text correct'), default=False)
    secondary_correct = models.BooleanField(_('secondary correct'), default=False)
    submitted_at = models.DateTimeField(_('submitted at'), auto_now_add=True)

    class Meta:
        verbose_name = _('guess')
        verbose_name_plural = _('guesses')
        ordering = ('round', 'submitted_at')
        constraints = [
            UniqueConstraint('round', 'player', name='unique_guess_per_round',
                             violation_error_message=_(
                                 'This player already answered this round.')),
        ]

    def __str__(self) -> str:
        return f'{self.player} - {self.text}'
