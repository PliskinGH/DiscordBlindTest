"""Cache invalidation of the writes to the guilds, hosts and player rows."""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .cache import bump, forget, guild_row_key, guild_scope, player_row_key
from .models import Guild, Host, Player


def _after_commit(function, *args) -> None:
    """Run an invalidation on the write it follows, once it is committed."""
    transaction.on_commit(lambda: function(*args))


@receiver(post_save, sender=Host)
@receiver(post_delete, sender=Host)
def _host_changed(sender, instance, **kwargs) -> None:
    """Drop the host list of the guild the host belongs to."""
    _after_commit(bump, guild_scope(instance.guild_id))


@receiver(post_save, sender=Player)
@receiver(post_delete, sender=Player)
def _player_changed(sender, instance, **kwargs) -> None:
    """Drop the cached row of the Discord account the player is met through."""
    if instance.discord_user_id:
        _after_commit(forget, player_row_key(instance.discord_user_id))


@receiver(post_save, sender=Guild)
@receiver(post_delete, sender=Guild)
def _guild_changed(sender, instance, **kwargs) -> None:
    """Drop the cached row of the guild the bot answered in."""
    _after_commit(forget, guild_row_key(instance.discord_id))
