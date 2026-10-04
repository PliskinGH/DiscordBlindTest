"""Discord OAuth2, and the REST reads the web admin needs beside it."""

import logging
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from discordcore.members import LocalUser

logger = logging.getLogger(__name__)

DISCORD_AUTHORIZE_URL = 'https://discord.com/oauth2/authorize'
DISCORD_API_BASE_URL = 'https://discord.com/api/v10'

# Session keys of the login flow.
STATE_SESSION_KEY = 'discord_oauth_state'
USER_SESSION_KEY = 'discord_user'
GUILDS_SESSION_KEY = 'discord_guilds'
ROLES_SESSION_KEY = 'discord_roles'

# Scopes asked for, and the Manage Server bit Discord reports per guild.
DISCORD_OAUTH_SCOPES = 'identify guilds'
MANAGE_GUILD = 0x20

# What the bot needs to play a quiz: View Channels, Send Messages, Embed Links
# and Send Messages in Threads (1<<10 | 1<<11 | 1<<14 | 1<<38).
BOT_PERMISSIONS = 274877926400

# The servers the bot is in, cached between dashboard renders.
BOT_GUILDS_KEY = 'webadmin:bot_guilds'
BOT_GUILDS_TIMEOUT = 60

DISCORD_OAUTH_DISABLED = _('Discord login is not configured.')
DISCORD_OAUTH_ERROR = _('Discord login failed. Please try again.')


def discord_oauth_configured() -> bool:
    """Return True when the Discord application credentials are set."""
    return bool(settings.DISCORD_CLIENT_ID and settings.DISCORD_CLIENT_SECRET)


def discord_redirect_uri(request) -> str:
    """Return the callback URI sent to Discord."""
    return settings.DISCORD_REDIRECT_URI or request.build_absolute_uri(
        reverse('webadmin:discord_callback'))


def invite_url(discord_guild_id: int) -> str:
    """Return the URL adding the bot to a server, or empty when unavailable."""
    if not settings.DISCORD_CLIENT_ID:
        return ''
    params = urlencode({
        'client_id': settings.DISCORD_CLIENT_ID,
        'scope': 'bot applications.commands',
        'permissions': BOT_PERMISSIONS,
        'guild_id': discord_guild_id,
        'disable_guild_select': 'true',
    })
    return f'{DISCORD_AUTHORIZE_URL}?{params}'


def fetch_bot_guild_ids() -> set[int]:
    """Return the servers the bot is in, read with its token.

    An empty set stands for "none known", which is also what an unreachable
    Discord gives back: the caller then offers the invite rather than a crash.
    """
    token = settings.DISCORD_TOKEN
    if not token:
        return set()
    cached = cache.get(BOT_GUILDS_KEY)
    if cached is not None:
        return set(cached)
    try:
        response = requests.get(f'{DISCORD_API_BASE_URL}/users/@me/guilds',
                                headers={'Authorization': f'Bot {token}'},
                                timeout=10)
        response.raise_for_status()
        guilds = {int(guild['id']) for guild in response.json()}
    except (requests.RequestException, KeyError, ValueError):
        logger.warning('Could not read the servers the bot is in',
                       exc_info=True)
        return set()
    cache.set(BOT_GUILDS_KEY, guilds, BOT_GUILDS_TIMEOUT)
    return guilds


def fetch_discord_identity(request, code: str) -> dict:
    """Exchange a code for the account it belongs to and the guilds it is in.

    Raises requests.RequestException, KeyError or ValueError on failure.
    """
    token = requests.post(
        f'{DISCORD_API_BASE_URL}/oauth2/token',
        data={
            'client_id': settings.DISCORD_CLIENT_ID,
            'client_secret': settings.DISCORD_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': discord_redirect_uri(request),
        },
        timeout=10,
    )
    token.raise_for_status()
    headers = {'Authorization': f'Bearer {token.json()["access_token"]}'}
    account = requests.get(f'{DISCORD_API_BASE_URL}/users/@me',
                           headers=headers, timeout=10)
    account.raise_for_status()
    guilds = requests.get(f'{DISCORD_API_BASE_URL}/users/@me/guilds',
                          headers=headers, timeout=10)
    guilds.raise_for_status()
    return {'account': account.json(), 'guilds': guilds.json()}


def fetch_member_roles(discord_guild_id: int, discord_user_id: int) -> list[int]:
    """Return the role ids a member holds, read with the bot token."""
    response = requests.get(
        f'{DISCORD_API_BASE_URL}/guilds/{discord_guild_id}'
        f'/members/{discord_user_id}',
        headers={'Authorization': f'Bot {settings.DISCORD_TOKEN}'}, timeout=10)
    response.raise_for_status()
    return [int(role_id) for role_id in response.json().get('roles', [])]


def account_of(identity: dict) -> LocalUser:
    """Return the Discord account the fetched identity describes."""
    account = identity['account']
    return LocalUser(id=int(account['id']),
                     name=account.get('username', ''))


def store_identity(session, identity: dict) -> None:
    """Keep the logged in account and the guilds it belongs to in the session."""
    account = identity['account']
    session[USER_SESSION_KEY] = {'id': int(account['id']),
                                 'username': account.get('username', '')}
    session[GUILDS_SESSION_KEY] = [
        {'id': int(guild['id']),
         'name': guild.get('name') or str(guild['id']),
         'permissions': int(guild.get('permissions') or 0)}
        for guild in identity['guilds']]


def session_guilds(session) -> list[dict]:
    """Return the guilds the logged in account belongs to."""
    return session.get(GUILDS_SESSION_KEY, [])


def session_guild(session, discord_guild_id: int) -> dict | None:
    """Return one guild of the logged in account, or None when it is not one."""
    for guild in session_guilds(session):
        if guild['id'] == discord_guild_id:
            return guild
    return None


def session_has_guild(session, discord_guild_id: int) -> bool:
    """Return True when the account belongs to the guild."""
    return session_guild(session, discord_guild_id) is not None


def session_permissions(session, discord_guild_id: int) -> int:
    """Return the account's permissions in a guild, or 0 when it is not in it."""
    for guild in session_guilds(session):
        if guild['id'] == discord_guild_id:
            return guild['permissions']
    return 0
