"""Discord OAuth2, and the REST reads the web admin needs beside it."""

import logging
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from discordcore.members import LocalUser
from discordcore.mentions import parse_mention
from discordcore.models import Player

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

# The channels, roles and members of a server, read on demand and kept briefly.
BOT_LIST_KEY = 'webadmin:bot_list:{}'
BOT_LIST_TIMEOUT = 300

# Channel types a quiz can be played in: text, news and the thread kinds.
PLAYABLE_CHANNEL_TYPES = frozenset({0, 5, 10, 11, 12})

# How many members a name is matched against before giving up.
MEMBER_SEARCH_LIMIT = 10

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


def _bot_get(path: str, params: dict | None = None) -> list[dict]:
    """Return the objects Discord lists for a bot-token request."""
    token = settings.DISCORD_TOKEN
    if not token:
        return []
    response = requests.get(f'{DISCORD_API_BASE_URL}{path}',
                            headers={'Authorization': f'Bot {token}'},
                            params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def _bot_list(key: str, path: str) -> list[dict]:
    """Return a bot-token list, reading Discord only while the cache is cold."""
    listed = cache.get(key)
    if listed is None:
        listed = _bot_get(path)
        cache.set(key, listed, BOT_LIST_TIMEOUT)
    return listed


def fetch_bot_channels(discord_guild_id: int) -> list[dict]:
    """Return the channels a quiz can be played in, as picker choices."""
    listed = _bot_list(BOT_LIST_KEY.format(f'channels:{discord_guild_id}'),
                       f'/guilds/{discord_guild_id}/channels')
    return [{'id': int(channel['id']),
             'label': f"#{channel.get('name') or channel['id']}"}
            for channel in listed
            if channel.get('type') in PLAYABLE_CHANNEL_TYPES]


def fetch_bot_roles(discord_guild_id: int) -> list[dict]:
    """Return the roles of a server, as picker choices.

    The everyone role and the roles Discord manages are left out: a game can
    mention neither.
    """
    listed = _bot_list(BOT_LIST_KEY.format(f'roles:{discord_guild_id}'),
                       f'/guilds/{discord_guild_id}/roles')
    return [{'id': int(role['id']),
             'label': f"@{role.get('name') or role['id']}"}
            for role in listed
            if int(role['id']) != discord_guild_id and not role.get('managed')]


def fetch_bot_member_by_id(discord_guild_id: int,
                           discord_user_id: int) -> dict | None:
    """Return a member of a server by id, or None when Discord has none.

    Raises requests.RequestException when Discord cannot be read, which is not
    the same as a member who has left the server.
    """
    key = BOT_LIST_KEY.format(f'member:{discord_guild_id}:{discord_user_id}')
    member = cache.get(key)
    if member is None:
        member = _bot_get(f'/guilds/{discord_guild_id}'
                          f'/members/{discord_user_id}')
        cache.set(key, member, BOT_LIST_TIMEOUT)
    return member or None


def fetch_bot_member(discord_guild_id: int, name: str) -> dict | None:
    """Return the member of a server whose name matches, or None when none does.

    An exact name wins; otherwise the first member Discord suggests is taken,
    so a partial name still resolves to somebody.
    """
    wanted = name.strip()
    if not wanted:
        return None
    listed = _bot_get(f'/guilds/{discord_guild_id}/members/search',
                      params={'query': wanted, 'limit': MEMBER_SEARCH_LIMIT})
    labelled = [(member, _member_label(member)) for member in listed]
    labelled = [(member, label) for member, label in labelled if label]
    if not labelled:
        return None
    exact = [pair for pair in labelled if pair[1].casefold() == wanted.casefold()]
    member, label = (exact or labelled)[0]
    user = member['user']
    return {'id': int(user['id']), 'label': label,
            'name': user.get('username') or label,
            'mention': f"<@{user['id']}>"}


def _member_label(member: dict) -> str:
    """Return the name a server member is shown under."""
    user = member.get('user') or {}
    return member.get('nick') or user.get('global_name') or user.get('username') or ''


def channel_label(discord_guild_id: int, channel_id: int | None) -> str:
    """Return how a stored channel is shown, with its name when Discord knows it."""
    return _label_of(fetch_bot_channels(discord_guild_id), channel_id, '#')


def role_label(discord_guild_id: int, role_id: int | None) -> str:
    """Return how a stored role is shown, with its name when Discord knows it."""
    return _label_of(fetch_bot_roles(discord_guild_id), role_id, '@')


def _label_of(choices: list[dict], value: int | None, prefix: str) -> str:
    """Return the label of a choice, falling back to the bare id."""
    if value is None:
        return ''
    for choice in choices:
        if choice['id'] == value:
            return choice['label']
    return f'{prefix}{value}'


def _account_name(member: dict) -> str:
    """Return the account name of a member, which is the one the bot keeps."""
    return (member.get('user') or {}).get('username') or ''


def remember_member(discord_user_id: int, name: str) -> None:
    """Keep the Discord name of an account, so no later read needs it again."""
    if name:
        Player.objects.from_discord(LocalUser(id=discord_user_id, name=name))


def member_label(discord_guild_id: int, discord_user_id: int) -> str:
    """Return how a member of a server is shown, with the name Discord knows.

    The name is remembered the first time it is learned, so a server whose
    hosts rarely change costs one read per member and none afterwards.
    """
    known = Player.objects.filter(discord_user_id=discord_user_id).first()
    if known and known.discord_name:
        return f'@{known.discord_name}'
    try:
        member = fetch_bot_member_by_id(discord_guild_id, discord_user_id)
    except requests.RequestException:
        logger.warning('Could not read member %s of server %s',
                       discord_user_id, discord_guild_id, exc_info=True)
        return ''
    if not member:
        return ''
    label = _member_label(member)
    remember_member(discord_user_id, _account_name(member) or label)
    return f'@{label}'


def mention_label(discord_guild_id: int, mention: str) -> str:
    """Return how a host mention is shown, with the name Discord knows it by."""
    try:
        is_role, discord_id = parse_mention(mention)
    except ValueError:
        return mention
    if is_role:
        return role_label(discord_guild_id, discord_id)
    return member_label(discord_guild_id, discord_id) or mention


def host_labels(discord_guild_id: int, mentions: list[str]) -> list[dict]:
    """Pair every host mention with the name to show, for the settings page."""
    return [{'mention': mention,
             'label': mention_label(discord_guild_id, mention)}
            for mention in mentions]


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
