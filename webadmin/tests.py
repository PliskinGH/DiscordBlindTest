"""Tests for the web admin's Discord login and landing pages."""

from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from blindtest import services
from discordcore.members import LocalMember, LocalPermissions, LocalRole
from discordcore.models import Guild, Host, Player

from . import discord_api

ACCOUNT = {'id': '42', 'username': 'hostie'}
GUILDS = [{'id': '7', 'name': 'Server Seven',
           'permissions': str(discord_api.MANAGE_GUILD)}]
IDENTITY = {'account': ACCOUNT, 'guilds': GUILDS}


class CacheTestCase(TestCase):
    """A test that starts from an empty cache, which outlives a rolled back row."""

    def setUp(self):
        super().setUp()
        cache.clear()


@override_settings(DISCORD_CLIENT_ID='cid', DISCORD_CLIENT_SECRET='secret')
class LoginTests(CacheTestCase):
    """The Discord login flow."""

    def test_connect_redirects_to_discord(self):
        response = self.client.get(reverse('webadmin:discord_connect'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(
            discord_api.DISCORD_AUTHORIZE_URL))
        self.assertIn(discord_api.STATE_SESSION_KEY, self.client.session)

    def test_callback_logs_the_player_in(self):
        session = self.client.session
        session[discord_api.STATE_SESSION_KEY] = 'state-1'
        session.save()
        with mock.patch.object(discord_api, 'fetch_discord_identity',
                               return_value=IDENTITY):
            response = self.client.get(reverse('webadmin:discord_callback'),
                                       {'code': 'abc', 'state': 'state-1'})
        self.assertRedirects(response, reverse('webadmin:dashboard'))
        player = Player.objects.get(discord_user_id=42)
        self.assertEqual(player.discord_name, 'hostie')
        self.assertEqual(self.client.session['_auth_user_id'], str(player.pk))
        self.assertEqual(self.client.session[discord_api.GUILDS_SESSION_KEY],
                         [{'id': 7, 'name': 'Server Seven',
                           'permissions': discord_api.MANAGE_GUILD}])

    def test_callback_refuses_a_bad_state(self):
        session = self.client.session
        session[discord_api.STATE_SESSION_KEY] = 'state-1'
        session.save()
        response = self.client.get(reverse('webadmin:discord_callback'),
                                   {'code': 'abc', 'state': 'other'})
        self.assertRedirects(response, reverse('webadmin:login'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_callback_reports_a_cancelled_login(self):
        response = self.client.get(reverse('webadmin:discord_callback'),
                                   {'error': 'access_denied'})
        self.assertRedirects(response, reverse('webadmin:login'))


class LoginPageTests(CacheTestCase):
    """The login page is the only public page."""

    def test_login_page_is_public(self):
        response = self.client.get(reverse('webadmin:login'))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse('webadmin:dashboard'))
        self.assertRedirects(
            response,
            f"{reverse('webadmin:login')}?next={reverse('webadmin:dashboard')}")


class IdentityTests(CacheTestCase):
    """The OAuth code exchange."""

    @override_settings(DISCORD_CLIENT_ID='cid', DISCORD_CLIENT_SECRET='secret')
    @mock.patch('webadmin.discord_api.requests')
    def test_fetch_discord_identity_reads_account_and_guilds(self, requests):
        requests.post.return_value.json.return_value = {'access_token': 'tok'}
        requests.get.side_effect = [
            mock.Mock(**{'json.return_value': ACCOUNT}),
            mock.Mock(**{'json.return_value': GUILDS}),
        ]
        request = RequestFactory().get(reverse('webadmin:discord_callback'))
        identity = discord_api.fetch_discord_identity(request, 'code')
        self.assertEqual(identity['account'], ACCOUNT)
        self.assertEqual(identity['guilds'], GUILDS)


class GuildPageTests(CacheTestCase):
    """A logged in member and the servers they may open."""

    def setUp(self):
        super().setUp()
        self.player = Player.objects.create_user(username='hostie',
                                                 discord_user_id=42)
        self.client.force_login(self.player)
        session = self.client.session
        session[discord_api.USER_SESSION_KEY] = {'id': 42, 'username': 'hostie'}
        session[discord_api.GUILDS_SESSION_KEY] = [
            {'id': 7, 'name': 'Server Seven',
             'permissions': discord_api.MANAGE_GUILD}]
        session.save()
        self.guild = Guild.objects.create(discord_id=7, name='Server Seven')

    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value=set())
    def test_dashboard_lists_the_server(self, bot_guilds):
        response = self.client.get(reverse('webadmin:dashboard'))
        self.assertContains(response, 'Server Seven')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_guild_page_shows_the_server(self, roles):
        response = self.client.get(reverse('webadmin:guild', args=[7]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Server Seven')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_guild_of_another_server_is_not_found(self, roles):
        response = self.client.get(reverse('webadmin:guild', args=[999]))
        self.assertEqual(response.status_code, 404)

    def test_guild_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('webadmin:guild', args=[7]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('webadmin:login'), response['Location'])


class MemberTests(CacheTestCase):
    """The OAuth member feeds the domain's host checks."""

    def setUp(self):
        super().setUp()
        self.guild = Guild.objects.create(discord_id=7, name='Server Seven')

    def _member(self, roles=(), manage=False):
        return LocalMember(
            id=42, name='hostie',
            roles=[LocalRole(role_id) for role_id in roles],
            guild_permissions=LocalPermissions(manage_guild=manage))

    def test_manage_guild_is_host(self):
        self.assertTrue(services.is_host(self.guild, self._member(manage=True)))

    def test_a_stranger_is_not_host(self):
        self.assertFalse(services.is_host(self.guild, self._member()))

    def test_a_mentioned_user_is_host(self):
        member = self._member()
        self.assertFalse(services.is_host(self.guild, member))
        with self.captureOnCommitCallbacks(execute=True):
            Host.objects.create(guild=self.guild, mention='<@42>')
        self.assertTrue(services.is_host(self.guild, member))

    def test_a_mentioned_role_is_host(self):
        Host.objects.create(guild=self.guild, mention='<@&99>')
        self.assertTrue(services.is_host(self.guild, self._member(roles=[99])))


@override_settings(DISCORD_CLIENT_ID='cid')
class AddGuildTests(CacheTestCase):
    """The dashboard offers the servers the bot is not registered in yet."""

    def setUp(self):
        self.player = Player.objects.create_user(username='hostie',
                                                 discord_user_id=42)
        self.client.force_login(self.player)
        session = self.client.session
        session[discord_api.USER_SESSION_KEY] = {'id': 42, 'username': 'hostie'}
        session[discord_api.GUILDS_SESSION_KEY] = [
            {'id': 7, 'name': 'Known Server',
             'permissions': discord_api.MANAGE_GUILD},
            {'id': 8, 'name': 'Bot Server',
             'permissions': discord_api.MANAGE_GUILD},
            {'id': 9, 'name': 'Botless Server',
             'permissions': discord_api.MANAGE_GUILD},
            {'id': 10, 'name': 'Quiet Server', 'permissions': 0},
        ]
        session.save()
        self.guild = Guild.objects.create(discord_id=7, name='Known Server')

    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value={8})
    def test_a_server_needs_no_rights_to_be_listed(self, bot_guilds):
        response = self.client.get(reverse('webadmin:dashboard'))
        self.assertContains(response, 'Known Server')
        self.assertContains(response, 'Bot Server')
        self.assertContains(response, 'Botless Server')
        self.assertNotContains(response, 'Quiet Server')

    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value={8})
    def test_a_server_the_bot_is_in_offers_add(self, bot_guilds):
        response = self.client.get(reverse('webadmin:dashboard'))
        self.assertContains(response, reverse('webadmin:guild_add', args=[8]))
        self.assertNotContains(response, reverse('webadmin:guild_add', args=[9]))

    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value={8})
    def test_a_server_the_bot_lacks_offers_the_invite(self, bot_guilds):
        response = self.client.get(reverse('webadmin:dashboard'))
        self.assertContains(response, 'Invite the bot')
        self.assertContains(response, 'guild_id=9')
        self.assertNotContains(response, 'guild_id=8')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value={8})
    def test_adding_a_server_registers_it(self, bot_guilds, roles):
        response = self.client.post(reverse('webadmin:guild_add', args=[8]))
        self.assertRedirects(response, reverse('webadmin:guild', args=[8]))
        self.assertTrue(Guild.objects.filter(discord_id=8).exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_server_the_bot_has_no_record_of_is_not_found(self, roles):
        response = self.client.get(reverse('webadmin:guild', args=[8]))
        self.assertEqual(response.status_code, 404)

    def test_adding_a_foreign_server_is_not_found(self):
        response = self.client.post(reverse('webadmin:guild_add', args=[99]))
        self.assertEqual(response.status_code, 404)

    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value=set())
    def test_adding_a_server_needs_manage_server(self, bot_guilds):
        response = self.client.post(reverse('webadmin:guild_add', args=[10]))
        self.assertRedirects(response, reverse('webadmin:dashboard'))
        self.assertFalse(Guild.objects.filter(discord_id=10).exists())
