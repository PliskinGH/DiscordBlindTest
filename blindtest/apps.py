from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BlindtestConfig(AppConfig):
    name = 'blindtest'
    verbose_name = _('blind test')

    def ready(self) -> None:
        from . import signals
