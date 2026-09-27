"""Discord mention strings: ``<@123>`` for a user, ``<@&456>`` for a role."""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MENTION_RE = re.compile(r'^<(?P<prefix>@!?|@&)(?P<discord_id>\d+)>$')

ROLE_PREFIX = '@&'

MENTION_ERROR = _('Enter a Discord mention, such as <@123456789> for a user '
                  'or <@&123456789> for a role.')


def parse_mention(mention: str | None) -> tuple[bool, int] | None:
    """Return ``(is_role, discord_id)`` for a mention, or None when malformed."""
    match = MENTION_RE.match((mention or '').strip())
    if match is None:
        return None
    return match['prefix'] == ROLE_PREFIX, int(match['discord_id'])


def user_mention(discord_id: int) -> str:
    """Return the mention of a Discord user."""
    return f'<@{discord_id}>'


def role_mention(discord_id: int) -> str:
    """Return the mention of a Discord role."""
    return f'<@&{discord_id}>'


def normalize_mention(mention: str | None) -> str:
    """Return the canonical form of a mention: ``<@!123>`` becomes ``<@123>``."""
    parsed = parse_mention(mention)
    if parsed is None:
        return (mention or '').strip()
    is_role, discord_id = parsed
    return role_mention(discord_id) if is_role else user_mention(discord_id)


def validate_mention(mention: str | None) -> None:
    """Raise ValidationError when the value is not a user or role mention."""
    if parse_mention(mention) is None:
        raise ValidationError(MENTION_ERROR, params={'value': mention})
