from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DiscordbotConfig(AppConfig):
    name = 'discordbot'
    verbose_name = _('Discord bot')
