from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DiscordcoreConfig(AppConfig):
    name = 'discordcore'
    verbose_name = _('Discord core')

    def ready(self) -> None:
        from . import signals
