"""Cache invalidation of the writes to the questions, rounds and games."""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from discordcore.cache import bump, guild_scope

from .caching import game_scope, library_scope, rounds_scope
from .models import Answer, AnswerVariant, Game, Question, Round


def _after_commit(function, *args) -> None:
    """Run an invalidation on the write it follows, once it is committed."""
    transaction.on_commit(lambda: function(*args))


@receiver(post_save, sender=Question)
@receiver(post_delete, sender=Question)
def _question_changed(sender, instance, **kwargs) -> None:
    """Drop the lists the question is offered in."""
    _after_commit(bump, library_scope(instance.guild_id))


@receiver(post_save, sender=Answer)
@receiver(post_delete, sender=Answer)
def _answer_changed(sender, instance, **kwargs) -> None:
    """Drop the lists holding a label the answer is written in."""
    _after_commit(bump, library_scope(instance.guild_id))


@receiver(post_save, sender=AnswerVariant)
@receiver(post_delete, sender=AnswerVariant)
def _variant_changed(sender, instance, **kwargs) -> None:
    """Drop the lists holding the answer the variant belongs to."""
    guild_id = (Answer.objects.filter(pk=instance.answer_id)
                .values_list('guild_id', flat=True).first())
    _after_commit(bump, library_scope(guild_id))


@receiver(post_save, sender=Round)
@receiver(post_delete, sender=Round)
def _round_changed(sender, instance, **kwargs) -> None:
    """Drop the queue and game lists of the guild the round is played in."""
    _after_commit(bump, game_scope(instance.game_id))
    guild_id = (Game.objects.filter(pk=instance.game_id)
                .values_list('guild_id', flat=True).first())
    if guild_id is not None:
        _after_commit(bump, rounds_scope(guild_id))


@receiver(post_save, sender=Game)
@receiver(post_delete, sender=Game)
def _game_changed(sender, instance, **kwargs) -> None:
    """Drop the game lists of the guild the game belongs to."""
    _after_commit(bump, guild_scope(instance.guild_id))
