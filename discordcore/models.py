from typing import Any

from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils.translation import gettext_lazy as _

from .cache import (STATE_TIMEOUT, guild_row_key, player_row_key, remember,
                    store)
from .members import DiscordGuild, DiscordUser
from .mentions import normalize_mention, parse_mention, validate_mention


class PlayerManager(UserManager):
    """Creates players from Discord accounts, which is how the bot meets them."""

    def from_discord(self, discord_user: DiscordUser) -> 'Player':
        """Return the player linked to ``discord_user``, creating it when missing.

        A cached row is served as is while it holds the name Discord reports.
        """
        key = player_row_key(discord_user.id)
        player = remember(key, lambda: self._sync(discord_user), STATE_TIMEOUT)
        if player.discord_name == (discord_user.name or None):
            return player
        return store(key, self._sync(discord_user), STATE_TIMEOUT)

    def _sync(self, discord_user: DiscordUser) -> 'Player':
        """Return the row of a Discord account, keeping its names up to date."""
        name = discord_user.name or None
        if name:
            self._release_stale(name, discord_user.id)
        player, created = self.get_or_create(
            discord_user_id=discord_user.id,
            defaults={
                'username': self._username_for(name, discord_user.id),
                'discord_name': name,
            },
        )
        if created or name is None or player.discord_name == name:
            return player
        update_fields = ['discord_name']
        discord_owned = (player.username == player.discord_name
                         or player.username
                         == f'discord_{player.discord_user_id}')
        if (discord_owned
                and not self.filter(username=name)
                             .exclude(pk=player.pk).exists()):
            player.username = name
            update_fields.append('username')
        player.discord_name = name
        player.save(update_fields=update_fields)
        return player

    def _release_stale(self, name: str, discord_user_id: int) -> None:
        """Free ``name`` from every row but the claimant's, as Discord did."""
        for stale in (self.filter(discord_name=name)
                          .exclude(discord_user_id=discord_user_id)):
            update_fields = ['discord_name']
            if stale.discord_user_id and stale.username == stale.discord_name:
                stale.username = f'discord_{stale.discord_user_id}'
                update_fields.append('username')
            stale.discord_name = None
            stale.save(update_fields=update_fields)

    def _username_for(self, name: str | None, discord_user_id: int) -> str:
        """Discord name at first meeting, or the id format when it is taken."""
        if name and not self.filter(username=name).exists():
            return name
        return f'discord_{discord_user_id}'


class Player(AbstractUser):
    """A blind test player, identified by their Discord account."""

    first_name = None
    last_name = None

    email = models.EmailField(_('email address'), unique=True,
                              blank=True, null=True, default=None)
    discord_user_id = models.BigIntegerField(_('Discord user ID'), unique=True,
                                             blank=True, null=True, default=None)
    discord_name = models.CharField(_('Discord username'), max_length=200,
                                    unique=True, blank=True, null=True,
                                    default=None)

    objects = PlayerManager()

    class Meta:
        verbose_name = _('player')
        verbose_name_plural = _('players')

    def __str__(self) -> str:
        return self.discord_name or self.username


class GuildManager(models.Manager):
    """Keeps the Discord servers the bot answered in."""

    def from_discord(self, discord_guild: DiscordGuild) -> 'Guild':
        """Return the guild known by the bot, creating it when missing.

        A cached row is served as is while it holds the name Discord reports.
        """
        key = guild_row_key(discord_guild.id)
        guild = remember(key, lambda: self._sync(discord_guild), STATE_TIMEOUT)
        if guild.name == discord_guild.name:
            return guild
        return store(key, self._sync(discord_guild), STATE_TIMEOUT)

    def _sync(self, discord_guild: DiscordGuild) -> 'Guild':
        """Return the row of a Discord server, refreshing its name."""
        guild, created = self.get_or_create(
            discord_id=discord_guild.id,
            defaults={'name': discord_guild.name},
        )
        if not created and guild.name != discord_guild.name:
            guild.name = discord_guild.name
            guild.save(update_fields=['name'])
        return guild


class Guild(models.Model):
    """A Discord server the bot knows about."""

    discord_id = models.BigIntegerField(_('Discord guild ID'), unique=True)
    name = models.CharField(_('name'), max_length=200, blank=True)
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    objects = GuildManager()

    class Meta:
        verbose_name = _('guild')
        verbose_name_plural = _('guilds')
        ordering = ('name', 'discord_id')

    def __str__(self) -> str:
        return self.name or str(self.discord_id)


class Host(models.Model):
    """A Discord user or role allowed to host blind tests in a guild."""

    guild = models.ForeignKey(Guild, on_delete=models.CASCADE,
                              related_name='hosts', verbose_name=_('guild'))
    mention = models.CharField(_('Discord mention'), max_length=100,
                               validators=[validate_mention],
                               help_text=_('A user mention (<@123456789>) or '
                                           'a role mention (<@&123456789>).'))
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        verbose_name = _('host')
        verbose_name_plural = _('hosts')
        ordering = ('guild', 'mention')
        constraints = [
            models.UniqueConstraint('guild', 'mention',
                                    name='unique_host_per_guild',
                                    violation_error_message=_(
                                        'This user or role is already a host '
                                        'of this server.')),
        ]

    def __str__(self) -> str:
        return self.mention

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.mention = normalize_mention(self.mention)
        super().save(*args, **kwargs)

    @property
    def is_role(self) -> bool:
        """True when the mention designates a role instead of a user."""
        parsed = parse_mention(self.mention)
        return bool(parsed and parsed[0])

    @property
    def discord_id(self) -> int | None:
        """The Discord ID held by the mention, or None when malformed."""
        parsed = parse_mention(self.mention)
        return parsed[1] if parsed else None
