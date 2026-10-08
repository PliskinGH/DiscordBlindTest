"""Tests for the web admin's Discord login and landing pages."""

import re
import requests
from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from blindtest import constants
from blindtest.models import (Broadcast, Game, Guess, Question, QuizType,
                              Round, ScoringMode)
from discordblindtest.testing import NetworkAccessDenied, NoNetworkMixin
from discordcore.members import LocalMember, LocalPermissions, LocalRole
from discordcore.models import Guild, Host, Player
from blindtest.services.broadcasts import (claim_broadcast, post_game_end,
                                           enqueue, game_broadcasts,
                                           mark_broadcast_failed,
                                           mark_broadcast_sent,
                                           pending_broadcasts,
                                           unfinished_broadcasts)
from blindtest.services.games import (active_game, control_state, create_game,
                                      end_game, played_game,
                                      queue_questions)
from blindtest.services.guessing import guess_of, submit_guess
from blindtest.services.guilds import is_host
from blindtest.services.rounds import current_round, round_guesses
from blindtest.services.teams import add_team

from . import discord_api, forms

ACCOUNT = {'id': '42', 'username': 'hostie'}
GUILDS = [{'id': '7', 'name': 'Server Seven',
           'permissions': str(discord_api.MANAGE_GUILD)}]
IDENTITY = {'account': ACCOUNT, 'guilds': GUILDS}

# The member Discord reports for the host of the server the tests set up.
WENDY = {'user': {'id': '42', 'username': 'wendy'}, 'nick': 'Wendy'}


class CacheTestCase(NoNetworkMixin, TestCase):
    """A test that starts from an empty cache, which outlives a rolled back row.

    It also inherits the shared block on the network, so a test that forgets to
    mock a Discord read fails here instead of calling the real API.
    """

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
                               return_value=IDENTITY), \
                mock.patch.object(discord_api, 'fetch_bot_guild_ids',
                                  return_value=set()):
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

    def test_the_guard_catches_a_forgotten_mock(self):
        """The network is blocked, so a forgotten mock fails loudly."""
        with self.assertRaises(NetworkAccessDenied):
            requests.get('https://discord.com/api/v10/users/@me')


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


class GuildSectionTests(CacheTestCase):
    """A host fills the library, an administrator manages the settings."""

    def setUp(self):
        super().setUp()
        self.player = Player.objects.create_user(
            username='hostie', discord_user_id=42, discord_name='hostie')
        self.client.force_login(self.player)
        self.guild = Guild.objects.create(discord_id=7, name='Server Seven')
        session = self.client.session
        session[discord_api.USER_SESSION_KEY] = {'id': 42, 'username': 'hostie'}
        session[discord_api.GUILDS_SESSION_KEY] = [
            {'id': 7, 'name': 'Server Seven',
             'permissions': discord_api.MANAGE_GUILD},
            {'id': 8, 'name': 'Other Server', 'permissions': 0},
        ]
        session.save()
        self.host = Host.objects.create(guild=self.guild, mention='<@42>')
        self.channels = [{'id': 555, 'label': '#quiz-lounge'},
                         {'id': 556, 'label': '#general'}]
        self.roles = [{'id': 99, 'label': '@Quizmaster'}]
        for name, given in (('fetch_bot_channels', self.channels),
                            ('fetch_bot_roles', self.roles),
                            ('fetch_bot_member', None),
                            ('fetch_bot_member_by_id', WENDY)):
            patcher = mock.patch(f'webadmin.discord_api.{name}',
                                 return_value=given)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _as_plain_member(self) -> None:
        """Drop the rights of the logged in player down to a plain member."""
        self.host.delete()
        session = self.client.session
        session[discord_api.GUILDS_SESSION_KEY] = [
            {'id': 7, 'name': 'Server Seven', 'permissions': 0}]
        session.save()

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_reaches_the_library(self, roles):
        response = self.client.get(reverse('webadmin:library', args=[7]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Add a question')
        self.assertContains(response, 'name="variants"')
        self.assertContains(response, 'type="submit"')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_plain_member_does_not_reach_the_library(self, roles):
        self._as_plain_member()
        response = self.client.get(reverse('webadmin:library', args=[7]))
        self.assertEqual(response.status_code, 403)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_does_not_reach_the_settings(self, roles):
        session = self.client.session
        session[discord_api.GUILDS_SESSION_KEY] = [
            {'id': 7, 'name': 'Server Seven', 'permissions': 0}]
        session.save()
        response = self.client.get(reverse('webadmin:settings', args=[7]))
        self.assertEqual(response.status_code, 403)

    def test_an_administrator_reaches_the_settings(self):
        response = self.client.get(reverse('webadmin:settings', args=[7]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '&lt;@42&gt;')
        # The pickers are fed the channels and roles Discord listed.
        self.assertContains(response, '#quiz-lounge')
        self.assertContains(response, '@Quizmaster')
        self.assertContains(response, 'django_select2')
        # Each form submits with its own button.
        self.assertContains(response, 'type="submit"')
        self.assertContains(response, 'value="Give host rights"')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_saves_a_question(self, roles):
        self.client.post(reverse('webadmin:question_add', args=[7]),
                         {'answer': 'Wonderwall', 'artist': 'Oasis',
                          'prompt': 'Guess it',
                          'variants': 'Wonderwall (Live),  Wonderwall - 1995'})
        question = Question.objects.get(guild=self.guild)
        self.assertEqual(question.expected_answer.text, 'Wonderwall')
        self.assertEqual(question.secondary_answer.text, 'Oasis')
        self.assertEqual(
            [variant.text
             for variant in question.expected_answer.variants.all()],
            ['Wonderwall - 1995', 'Wonderwall (Live)'])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_edits_the_variants_of_a_question(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Song'))
        self.client.post(
            reverse('webadmin:question_edit', args=[7, question.pk]),
            {'answer': 'Song', 'artist': '', 'prompt': '',
             'choices': '', 'year': '', 'media': '',
             'variants': 'Tune, Air'})
        answer = self.guild.answers.get(text='Song')
        self.assertEqual([variant.text for variant in answer.variants.all()],
                         ['Air', 'Tune'])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_removes_every_variant_of_a_question(self, roles):
        answer = self.guild.answers.create(text='Song')
        question = self.guild.questions.create(expected_answer=answer)
        url = reverse('webadmin:question_edit', args=[7, question.pk])
        filled = {'answer': 'Song', 'artist': '', 'prompt': '', 'choices': '',
                  'year': '', 'media': ''}
        self.client.post(url, filled | {'variants': 'Tune'})
        self.assertEqual(answer.variants.count(), 1)
        self.client.post(url, filled | {'variants': ''})
        self.assertEqual(answer.variants.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_an_edit_form_without_an_answer_is_refused(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Mine'))
        response = self.client.post(
            reverse('webadmin:question_edit', args=[7, question.pk]),
            {'answer': '', 'artist': '', 'prompt': '', 'choices': '',
             'year': '', 'media': '', 'variants': ''}, follow=True)
        self.assertContains(response, 'A question needs an answer.')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_library_lists_only_this_servers_questions(self, roles):
        self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Mine'))
        other = Guild.objects.create(discord_id=8, name='Other Server')
        other.questions.create(
            expected_answer=other.answers.create(text='Theirs'))
        response = self.client.get(reverse('webadmin:library', args=[7]))
        self.assertContains(response, 'Mine')
        self.assertNotContains(response, 'Theirs')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_library_offers_what_can_go(self, roles):
        self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Mine'))
        self.guild.answers.create(text='Spare')
        response = self.client.get(reverse('webadmin:library', args=[7]))
        self.assertContains(response, 'Not used yet')
        self.assertContains(response, 'Remove the question')
        self.assertContains(response, 'Remove the answer')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_removes_an_unused_question(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Mine'))
        self.client.post(
            reverse('webadmin:question_remove', args=[7, question.pk]))
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_removes_an_unused_answer(self, roles):
        answer = self.guild.answers.create(text='Spare')
        self.client.post(reverse('webadmin:answer_remove', args=[7, answer.pk]))
        self.assertFalse(self.guild.answers.filter(pk=answer.pk).exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_removing_an_answer_a_question_uses_reports_why(self, roles):
        answer = self.guild.answers.create(text='Mine')
        self.guild.questions.create(prompt='Guess it', expected_answer=answer)
        response = self.client.post(
            reverse('webadmin:answer_remove', args=[7, answer.pk]), follow=True)
        self.assertContains(response, 'used by the question')
        self.assertTrue(self.guild.answers.filter(pk=answer.pk).exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_stranger_removes_nothing(self, roles):
        self._as_plain_member()
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Mine'))
        self.client.post(
            reverse('webadmin:question_remove', args=[7, question.pk]))
        self.assertTrue(Question.objects.filter(pk=question.pk).exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_edits_a_question(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Old'),
            album='Old album')
        response = self.client.get(
            reverse('webadmin:question_edit', args=[7, question.pk]))
        self.assertEqual(response.status_code, 200)
        self.client.post(
            reverse('webadmin:question_edit', args=[7, question.pk]),
            {'answer': 'New', 'artist': '', 'prompt': '', 'choices': '',
             'year': '', 'media': '', 'album': 'New album', 'variants': ''})
        question.refresh_from_db()
        self.assertEqual(question.expected_answer.text, 'New')
        self.assertEqual(question.album, 'New album')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_clears_a_field_by_emptying_it(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Old'),
            album='Old album', prompt='Old prompt')
        self.client.post(
            reverse('webadmin:question_edit', args=[7, question.pk]),
            {'answer': 'Old', 'artist': '', 'prompt': '', 'choices': '',
             'year': '', 'media': '', 'album': '', 'variants': ''})
        question.refresh_from_db()
        self.assertEqual(question.album, '')
        self.assertEqual(question.prompt, '')

    def _form_actions(self, page: str) -> set[str]:
        """Return the URLs the forms of a page post to, the logout one aside."""
        html = self.client.get(page).content.decode()
        actions = set(re.findall(r'<form[^>]*action="([^"]*)"', html))
        return actions - {reverse('webadmin:logout')}

    def _fields_of(self, page: str, action: str) -> list[str]:
        """Return the fields a page asks for, in the order it renders them.

        Scoped to the form posting to ``action``, since a page holds several.
        """
        html = self.client.get(page).content.decode()
        form = next(part for part in re.findall(r'<form.*?</form>', html, re.S)
                    if f'action="{action}"' in part)
        return [name for name in re.findall(
            r'<input(?![^>]*type="hidden")[^>]*name="([^"]+)"', form)
            if name != 'save']

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_prompt_comes_first_on_the_library_page(self, roles):
        page = reverse('webadmin:library', args=[7])
        self.assertEqual(
            self._fields_of(page, reverse('webadmin:question_add', args=[7])),
            ['prompt', 'answer', 'artist', 'variants', 'choices', 'year',
             'album', 'media'])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_prompt_comes_first_on_the_edit_page(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Old'))
        page = reverse('webadmin:question_edit', args=[7, question.pk])
        fields = self._fields_of(page, page)
        self.assertEqual(fields[:2], ['prompt', 'answer'])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_page_keeps_its_form_fields_out_of_the_head(self, roles):
        # ``{{ form.media }}`` in a template finds the media field, not Media.
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Old'))
        for page in (reverse('webadmin:library', args=[7]),
                     reverse('webadmin:question_edit', args=[7, question.pk])):
            html = self.client.get(page).content.decode()
            head = html.split('</head>')[0]
            self.assertNotIn('<input', head)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_cancel_button_sits_beside_save(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Old'))
        page = reverse('webadmin:question_edit', args=[7, question.pk])
        response = self.client.get(page)
        library = reverse('webadmin:library', args=[7])
        self.assertContains(response, '<a class="btn btn-outline-secondary '
                                     f'ms-2" href="{library}">Cancel</a>')
        # Both belong to the form, so the buttons share one row.
        form = next(part for part in re.findall(r'<form.*?</form>',
                                                response.content.decode(),
                                                re.S)
                    if f'action="{page}"' in part)
        self.assertIn('type="submit"', form)
        self.assertIn('btn-outline-secondary ms-2', form)

    def _post_as_the_browser_does(self, page: str, ending: str,
                                  data: dict):
        """Submit the form of a page whose action ends as told."""
        action = next(url for url in self._form_actions(page)
                      if url.endswith(ending))
        return self.client.post(action, data)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_settings_forms_post_to_their_own_handlers(self, roles):
        page = reverse('webadmin:settings', args=[7])
        actions = self._form_actions(page)
        for route in ('setting_channel', 'setting_ping', 'setting_hosts'):
            self.assertIn(reverse(f'webadmin:{route}', args=[7]), actions)
        self.assertNotIn(page, actions)

    def test_the_channel_form_saves_the_channel_it_posts(self):
        self._post_as_the_browser_does(
            reverse('webadmin:settings', args=[7]), '/channel/',
            {'channel_id': '555'})
        self.guild.refresh_from_db()
        self.assertEqual(self.guild.default_channel_id, 555)

    def test_the_ping_form_saves_the_role_it_posts(self):
        self._post_as_the_browser_does(
            reverse('webadmin:settings', args=[7]), '/ping/', {'role_id': '99'})
        self.guild.refresh_from_db()
        self.assertEqual(self.guild.default_ping_role_id, 99)

    def test_the_host_form_saves_the_role_it_posts(self):
        self._post_as_the_browser_does(
            reverse('webadmin:settings', args=[7]), '/hosts/',
            {'role_id': '99'})
        self.assertTrue(self.guild.hosts.filter(mention='<@&99>').exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_question_form_posts_to_its_own_handler(self, roles):
        page = reverse('webadmin:library', args=[7])
        self.assertEqual(self._form_actions(page),
                         {reverse('webadmin:question_add', args=[7])})
        self._post_as_the_browser_does(
            page, '/library/questions/', {'answer': 'Wonderwall'})
        self.assertTrue(
            self.guild.answers.filter(text='Wonderwall').exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_edit_form_posts_to_its_own_handler(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Old'))
        page = reverse('webadmin:question_edit', args=[7, question.pk])
        self.assertEqual(self._form_actions(page), {page})
        self._post_as_the_browser_does(page, '/', {'answer': 'New'})
        question.refresh_from_db()
        self.assertEqual(question.expected_answer.text, 'New')

    def test_an_administrator_sets_the_defaults(self):
        self.client.post(reverse('webadmin:setting_channel', args=[7]),
                         {'channel_id': '555'})
        self.client.post(reverse('webadmin:setting_ping', args=[7]),
                         {'role_id': '99'})
        self.guild.refresh_from_db()
        self.assertEqual(self.guild.default_channel_id, 555)
        self.assertEqual(self.guild.default_ping_role_id, 99)

    def test_the_settings_name_what_was_set(self):
        self.client.post(reverse('webadmin:setting_channel', args=[7]),
                         {'channel_id': '555'})
        self.client.post(reverse('webadmin:setting_ping', args=[7]),
                         {'role_id': '99'})
        response = self.client.get(reverse('webadmin:settings', args=[7]))
        self.assertContains(response, '#quiz-lounge')
        self.assertContains(response, '@Quizmaster')

    def test_a_channel_discord_does_not_know_shows_its_id(self):
        self.guild.default_channel_id = 404
        self.guild.save(update_fields=['default_channel_id'])
        response = self.client.get(reverse('webadmin:settings', args=[7]))
        self.assertContains(response, '#404')

    def test_the_settings_page_repeats_no_element_id(self):
        html = self.client.get(
            reverse('webadmin:settings', args=[7])).content.decode()
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual([name for name in ids if ids.count(name) > 1], [])

    def test_the_pickers_start_on_what_was_saved(self):
        self.guild.default_channel_id = 555
        self.guild.default_ping_role_id = 99
        self.guild.save(update_fields=['default_channel_id',
                                       'default_ping_role_id'])
        response = self.client.get(reverse('webadmin:settings', args=[7]))
        self.assertContains(response, '<option value="555" selected>')
        self.assertContains(response, '<option value="99" selected>')

    def test_a_picker_starts_on_nothing_when_nothing_is_saved(self):
        html = self.client.get(
            reverse('webadmin:settings', args=[7])).content.decode()
        self.assertNotIn('<option value="555" selected>', html)

    def test_a_picker_keeps_a_value_discord_dropped(self):
        self.guild.default_channel_id = 404
        self.guild.save(update_fields=['default_channel_id'])
        response = self.client.get(reverse('webadmin:settings', args=[7]))
        self.assertContains(response, '<option value="404" selected>#404')
        # Saving the page again must not refuse the value it showed.
        self._post_as_the_browser_does(
            reverse('webadmin:settings', args=[7]), '/channel/',
            {'channel_id': '404'})
        self.guild.refresh_from_db()
        self.assertEqual(self.guild.default_channel_id, 404)

    def test_the_hosts_are_named(self):
        Host.objects.create(guild=self.guild, mention='<@&99>')
        response = self.client.get(reverse('webadmin:settings', args=[7]))
        self.assertContains(response, '@hostie')
        self.assertContains(response, '@Quizmaster')

    def test_the_host_list_still_posts_the_mention(self):
        response = self.client.get(reverse('webadmin:settings', args=[7]))
        self.assertContains(response, 'name="mention" value="&lt;@42&gt;"')

    def test_a_known_host_is_named_without_asking_discord(self):
        with mock.patch('webadmin.discord_api.fetch_bot_member_by_id') as read:
            self.client.get(reverse('webadmin:settings', args=[7]))
        read.assert_not_called()

    def test_a_legacy_host_is_named_once_and_then_remembered(self):
        Host.objects.create(guild=self.guild, mention='<@77>')
        with mock.patch('webadmin.discord_api.fetch_bot_member_by_id',
                        return_value=WENDY) as read:
            response = self.client.get(reverse('webadmin:settings', args=[7]))
            self.assertContains(response, '@Wendy')
            self.assertEqual(read.call_count, 1)
            self.client.get(reverse('webadmin:settings', args=[7]))
            self.assertEqual(read.call_count, 1)
        self.assertEqual(
            Player.objects.get(discord_user_id=77).discord_name, 'wendy')

    def test_a_host_named_on_saving_is_kept_for_the_next_page(self):
        with mock.patch('webadmin.discord_api.fetch_bot_member',
                        return_value={'id': 77, 'label': 'Wendy',
                                      'name': 'wendy', 'mention': '<@77>'}):
            self.client.post(reverse('webadmin:setting_hosts', args=[7]),
                             {'mention': 'Wendy'})
        self.assertTrue(
            Player.objects.filter(discord_user_id=77,
                                  discord_name='wendy').exists())

    def test_a_picker_refuses_an_option_it_was_not_given(self):
        self.client.post(reverse('webadmin:setting_channel', args=[7]),
                         {'channel_id': '999'})
        self.assertIsNone(self.guild.default_channel_id)

    def test_an_administrator_clears_the_defaults(self):
        self.client.post(reverse('webadmin:setting_channel', args=[7]),
                         {'channel_id': '555'})
        self.client.post(reverse('webadmin:setting_channel', args=[7]),
                         {'action': 'clear'})
        self.guild.refresh_from_db()
        self.assertIsNone(self.guild.default_channel_id)

    def test_an_administrator_manages_the_hosts(self):
        self.client.post(reverse('webadmin:setting_hosts', args=[7]),
                         {'role_id': '99'})
        self.assertTrue(self.guild.hosts.filter(mention='<@&99>').exists())
        self.client.post(reverse('webadmin:setting_hosts', args=[7]),
                         {'mention': '<@&99>', 'action': 'remove'})
        self.assertFalse(self.guild.hosts.filter(mention='<@&99>').exists())

    def test_an_administrator_names_a_member_to_host(self):
        with mock.patch('webadmin.discord_api.fetch_bot_member',
                        return_value={'id': 77, 'label': 'Wendy',
                                      'mention': '<@77>'}):
            self.client.post(reverse('webadmin:setting_hosts', args=[7]),
                             {'mention': 'Wendy'})
        self.assertTrue(self.guild.hosts.filter(mention='<@77>').exists())

    def test_an_administrator_names_nobody_at_all(self):
        response = self.client.post(reverse('webadmin:setting_hosts', args=[7]),
                                    {'mention': 'Nobody'}, follow=True)
        self.assertContains(response, 'No member named')
        self.assertEqual(self.guild.hosts.count(), 1)

    def test_a_refused_change_reports_why(self):
        response = self.client.post(reverse('webadmin:setting_hosts', args=[7]),
                                    {'mention': 'not a mention'}, follow=True)
        self.assertContains(response, 'No member named')


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
        self.assertTrue(is_host(self.guild, self._member(manage=True)))

    def test_a_stranger_is_not_host(self):
        self.assertFalse(is_host(self.guild, self._member()))

    def test_a_mentioned_user_is_host(self):
        member = self._member()
        self.assertFalse(is_host(self.guild, member))
        with self.captureOnCommitCallbacks(execute=True):
            Host.objects.create(guild=self.guild, mention='<@42>')
        self.assertTrue(is_host(self.guild, member))

    def test_a_mentioned_role_is_host(self):
        Host.objects.create(guild=self.guild, mention='<@&99>')
        self.assertTrue(is_host(self.guild, self._member(roles=[99])))


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


class GameControlTests(CacheTestCase):
    """A host runs a game from the browser, and everyone guesses from theirs."""

    def setUp(self):
        super().setUp()
        self.host_player = Player.objects.create_user(
            username='hostie', email='hostie@example.com',
            discord_user_id=42, discord_name='hostie')
        self.guest = Player.objects.create_user(
            username='guestie', email='guestie@example.com',
            discord_user_id=43, discord_name='guestie')
        self.client.force_login(self.host_player)
        self.guild = Guild.objects.create(discord_id=7, name='Server Seven')
        session = self.client.session
        session[discord_api.USER_SESSION_KEY] = {'id': 42, 'username': 'hostie'}
        session[discord_api.GUILDS_SESSION_KEY] = [
            {'id': 7, 'name': 'Server Seven',
             'permissions': discord_api.MANAGE_GUILD}]
        session.save()
        self.host = Host.objects.create(guild=self.guild, mention='<@42>')
        self.member = LocalMember(id=42, name='hostie')
        for name, given in (('fetch_bot_channels',
                             [{'id': 555, 'label': '#quiz-lounge'}]),
                            ('fetch_bot_roles',
                             [{'id': 99, 'label': '@Quizmaster'}]),
                            ('fetch_member_roles', [])):
            patcher = mock.patch(f'webadmin.discord_api.{name}',
                                 return_value=given)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _other_guild(self):
        """Return another server, one this session may not reach."""
        return Guild.objects.get_or_create(
            discord_id=8, defaults={'name': 'Other Server'})[0]

    def _question(self, text='Wonderwall', artist='', prompt='Guess it'):
        """Return a question of this server, playable as a blind test round."""
        question = self.guild.questions.create(
            prompt=prompt, expected_answer=self.guild.answers.create(text=text),
            secondary_answer=(self.guild.answers.create(text=artist)
                              if artist else None))
        return question

    def _choice_question(self):
        """Return a question whose expected answer is one of its choices."""
        question = self._question()
        other = self.guild.answers.create(text='Yesterday')
        question.choices.add(question.expected_answer, other)
        return question

    def _game(self, state=Game.State.RUNNING, quiz_type=QuizType.BLIND_TEST):
        """Return a game of this server, created the way Discord creates one."""
        return create_game(self.guild, 555, self.member,
                                    quiz_type=quiz_type, state=state)

    def _queue(self, game, question):
        """Queue one question of the game."""
        return queue_questions(game, self.member, [question.pk])

    def _url(self, name, *args) -> str:
        """Return a control room route of this server."""
        return reverse(f'webadmin:{name}', args=[7, *args])

    def _member_by_id(self, discord_user_id: int, name: str) -> Player:
        """Register the player a Discord member lookup resolves to."""
        return Player.objects.create_user(
            username=name + str(discord_user_id),
            email=name + str(discord_user_id) + '@example.com',
            discord_user_id=discord_user_id, discord_name=name)


    @mock.patch('webadmin.discord_api.fetch_bot_member')
    def test_a_host_creates_a_team_with_its_first_member(self, member):
        guest = self._member_by_id(77, 'Wendy')
        member.return_value = {'id': 77, 'label': 'Wendy',
                               'name': 'wendy', 'mention': '<@77>'}
        game = self._game()
        response = self.client.post(self._url('team_add', game.pk),
                                    {'name': 'Reds', 'member': 'Wendy'},
                                    follow=True)
        self.assertContains(response, 'Reds added to this game.')
        self.assertEqual(list(game.teams.get().players.all()), [guest])

    @mock.patch('webadmin.discord_api.fetch_bot_member')
    def test_a_host_creates_an_empty_team(self, member):
        game = self._game()
        self.client.post(self._url('team_add', game.pk), {'name': 'Blues'})
        self.assertEqual([team.name for team in game.teams.all()],
                         ['Blues'])
        member.assert_not_called()

    @mock.patch('webadmin.discord_api.fetch_bot_member')
    def test_a_host_pastes_a_member_mention(self, member):
        self._member_by_id(77, 'Wendy')
        game = self._game()
        self.client.post(self._url('team_add', game.pk),
                         {'name': 'Reds', 'member': '<@77>'})
        self.assertEqual(len(game.teams.get().players.all()), 1)
        member.assert_not_called()

    @mock.patch('webadmin.discord_api.fetch_bot_member', return_value=None)
    def test_a_host_names_nobody_at_all(self, member):
        game = self._game()
        response = self.client.post(self._url('team_add', game.pk),
                                    {'name': 'Reds', 'member': 'Nobody'},
                                    follow=True)
        self.assertContains(response, 'No member named')
        self.assertEqual(game.teams.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_bot_member')
    def test_a_discord_that_cannot_be_reached_is_reported(self, member):
        member.side_effect = requests.RequestException
        game = self._game()
        response = self.client.post(self._url('team_add', game.pk),
                                    {'name': 'Reds', 'member': 'Wendy'},
                                    follow=True)
        self.assertContains(response, 'Discord could not be reached')
        self.assertEqual(game.teams.count(), 0)

    def test_a_duplicated_team_name_is_refused(self):
        game = self._game()
        self.client.post(self._url('team_add', game.pk), {'name': 'Reds'})
        response = self.client.post(self._url('team_add', game.pk),
                                    {'name': 'reds'}, follow=True)
        self.assertContains(response, 'already has a team called')
        self.assertEqual(game.teams.count(), 1)

    @mock.patch('webadmin.discord_api.fetch_bot_member')
    def test_a_host_puts_a_member_in_a_team(self, member):
        self._member_by_id(77, 'Wendy')
        member.return_value = {'id': 77, 'label': 'Wendy',
                               'name': 'wendy', 'mention': '<@77>'}
        game = self._game()
        team = add_team(game, self.member, 'Reds')
        self.client.post(self._url('team_member', game.pk, team.pk),
                         {'member': 'Wendy'})
        self.assertEqual(len(team.players.all()), 1)

    def test_a_host_takes_a_member_out_of_a_team(self):
        game = self._game()
        team = add_team(game, self.member, 'Reds', [self.guest])
        self.client.post(self._url('team_member', game.pk, team.pk),
                         {'action': 'remove', 'player': self.guest.pk})
        self.assertEqual(list(team.players.all()), [])

    def test_a_host_renames_a_team(self):
        game = self._game()
        team = add_team(game, self.member, 'Reds')
        self.client.post(self._url('team_rename', game.pk, team.pk),
                         {'name': 'Crimson'})
        team.refresh_from_db()
        self.assertEqual(team.name, 'Crimson')

    def test_a_host_removes_a_team(self):
        game = self._game()
        team = add_team(game, self.member, 'Reds')
        self.client.post(self._url('team_remove', game.pk, team.pk))
        self.assertEqual(game.teams.count(), 0)

    def test_a_team_of_another_game_is_not_reachable(self):
        ended, other = self._ended_game_with_a_team()
        game = self._game()
        add_team(game, self.member, 'Reds')
        response = self.client.post(self._url('team_remove', game.pk, other.pk))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ended.teams.count(), 1)

    def _ended_game_with_a_team(self):
        """Return an ended game of this server and one of its teams."""
        ended = create_game(self.guild, 555, self.member,
                            state=Game.State.SETUP)
        team = add_team(ended, self.member, 'Blues')
        end_game(ended, self.member)
        return ended, team

    def _team_of_another_server(self):
        """Return a team of a server the session may not reach."""
        other = self._other_guild()
        Host.objects.get_or_create(guild=other, mention='<@42>')
        return add_team(create_game(other, 555, self.member), self.member,
                        'Reds')

    def test_the_teams_of_a_finished_game_are_frozen(self):
        game = self._game()
        add_team(game, self.member, 'Reds')
        end_game(game, self.member)
        response = self.client.post(self._url('team_add', game.pk),
                                    {'name': 'Blues'}, follow=True)
        self.assertContains(response, 'This game is over')
        self.assertEqual(game.teams.count(), 1)

    def test_a_stranger_does_not_manage_the_teams(self):
        game = self._game()
        team = add_team(game, self.member, 'Reds')
        self._as_guest()
        posts = [(self._url('team_add', game.pk), {}),
                 (self._url('team_member', game.pk, team.pk), {}),
                 (self._url('team_rename', game.pk, team.pk), {'name': 'X'}),
                 (self._url('team_remove', game.pk, team.pk), {})]
        for url, data in posts:
            with self.subTest(url=url):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 403)

    def test_the_control_room_lists_the_teams_with_their_members(self):
        game = self._game()
        add_team(game, self.member, 'Reds', [self.guest])
        add_team(game, self.member, 'Blues')
        response = self.client.get(self._url('game', game.pk))
        self.assertContains(response, 'Reds')
        self.assertContains(response, 'guestie')
        self.assertContains(response, 'Nobody has guessed for this team yet.')

    def test_a_host_copies_a_team_of_another_game_with_its_members(self):
        past = self._ended_game_with_a_team()[1]
        game = self._game()
        response = self.client.post(self._url('team_copy', game.pk),
                                    {'source': past.pk}, follow=True)
        self.assertContains(response, 'Blues copied into this game')
        copied = game.teams.get()
        self.assertEqual(copied.name, 'Blues')
        self.assertEqual(list(copied.players.all()), [])

    def test_a_copied_team_brings_the_members_of_the_original(self):
        past = self._ended_game_with_a_team()[1]
        past.players.add(self.guest)
        game = self._game()
        self.client.post(self._url('team_copy', game.pk), {'source': past.pk})
        self.assertEqual(list(game.teams.get().players.all()), [self.guest])

    def test_a_game_refuses_to_copy_a_team_it_already_has(self):
        past = self._ended_game_with_a_team()[1]
        game = self._game()
        add_team(game, self.member, 'Blues')
        response = self.client.post(self._url('team_copy', game.pk),
                                    {'source': past.pk}, follow=True)
        self.assertContains(response, 'already has a team called')
        self.assertEqual(game.teams.count(), 1)

    def test_a_game_refuses_to_copy_one_of_its_own_teams(self):
        game = self._game()
        team = add_team(game, self.member, 'Reds')
        response = self.client.post(self._url('team_copy', game.pk),
                                    {'source': team.pk}, follow=True)
        self.assertContains(response, 'Select a valid choice')
        self.assertEqual(game.teams.count(), 1)

    def test_a_game_refuses_to_copy_a_team_of_another_server(self):
        game = self._game()
        other = self._team_of_another_server()
        response = self.client.post(self._url('team_copy', game.pk),
                                    {'source': other.pk}, follow=True)
        self.assertContains(response, 'Select a valid choice')
        self.assertEqual(game.teams.count(), 0)

    def test_a_finished_game_does_not_take_a_copied_team(self):
        past = self._ended_game_with_a_team()[1]
        game = self._game()
        end_game(game, self.member)
        response = self.client.post(self._url('team_copy', game.pk),
                                    {'source': past.pk}, follow=True)
        self.assertContains(response, 'This game is over')
        self.assertEqual(game.teams.count(), 0)

    def test_a_stranger_does_not_copy_a_team(self):
        past = self._ended_game_with_a_team()[1]
        game = self._game()
        self._as_guest()
        response = self.client.post(self._url('team_copy', game.pk),
                                    {'source': past.pk})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(game.teams.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_copy_picker_searches_the_teams_of_the_other_games(self, roles):
        past = self._ended_game_with_a_team()[1]
        game = self._game()
        add_team(game, self.member, 'Reds')
        page = self.client.get(self._url('game', game.pk))
        field = self._field_id(page, 'copy_team_source')
        results = self._search(game, field, 'Blues').json()['results']
        self.assertEqual([row['id'] for row in results], [past.pk])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_copy_picker_never_offers_a_team_of_the_game_itself(self, roles):
        self._ended_game_with_a_team()
        game = self._game()
        add_team(game, self.member, 'Reds')
        page = self.client.get(self._url('game', game.pk))
        field = self._field_id(page, 'copy_team_source')
        results = self._search(game, field, 'Reds').json()['results']
        self.assertEqual(results, [])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_copy_picker_is_searched_by_the_game_a_team_played_in(self, roles):
        past = self._ended_game_with_a_team()[1]
        past.game.name = 'Friday quiz'
        past.game.save(update_fields=['name'])
        game = self._game()
        page = self.client.get(self._url('game', game.pk))
        results = self._search(game, self._field_id(page, 'copy_team_source'),
                               'Friday').json()['results']
        self.assertEqual([row['id'] for row in results], [past.pk])
        self.assertIn('Friday quiz — Blues', results[0]['text'])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_copy_picker_never_offers_a_team_of_another_server(self, roles):
        game = self._game()
        self._team_of_another_server()
        page = self.client.get(self._url('game', game.pk))
        results = self._search(game, self._field_id(page, 'copy_team_source'),
                               'Reds').json()['results']
        self.assertEqual(results, [])

    def test_the_control_room_says_when_a_game_has_no_team(self):
        response = self.client.get(self._url('game', self._game().pk))
        self.assertContains(response, 'No team yet')

    def _as_guest(self) -> None:
        """Log in as a member of the server who holds no host rights."""
        self.host.delete()
        self.client.force_login(self.guest)
        session = self.client.session
        session[discord_api.USER_SESSION_KEY] = {'id': 43, 'username': 'guestie'}
        session[discord_api.GUILDS_SESSION_KEY] = [
            {'id': 7, 'name': 'Server Seven', 'permissions': 0}]
        session.save()

    def _setup_data(self, **over) -> dict:
        """Return the fields the setup form is submitted with."""
        return {'channel_id': '555', 'ping_role_id': '', 'name': '',
                'quiz_type': QuizType.BLIND_TEST,
                'scoring_mode': ScoringMode.STANDARD} | over

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_sets_a_quiz_up_without_announcing_it(self, roles):
        response = self.client.post(self._url('game_setup'),
                                    self._setup_data(name='Friday quiz'))
        game = Game.objects.get()
        self.assertRedirects(response, self._url('game', game.pk))
        self.assertEqual(game.state, Game.State.SETUP)
        self.assertEqual(game.channel_id, 555)
        self.assertEqual(game.name, 'Friday quiz')
        # Nothing is owed to the server: no round opened, no game published.
        self.assertEqual(Broadcast.objects.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_published_game_leaves_a_post_to_the_worker(self, roles):
        game = self._game(state=Game.State.SETUP)
        self.client.post(self._url('game_publish', game.pk))
        game.refresh_from_db()
        self.assertEqual(game.state, Game.State.RUNNING)
        [broadcast] = Broadcast.objects.all()
        self.assertEqual(broadcast.kind, Broadcast.Kind.PUBLISH)
        # The worker claims whatever a caller without a client recorded.
        self.assertEqual(broadcast.status, Broadcast.Status.PENDING)
        self.assertIn(broadcast, pending_broadcasts())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_next_round_leaves_a_post_to_the_worker(self, roles):
        question = self._question()
        game = self._game()
        self._queue(game, question)
        self.client.post(self._url('game_next', game.pk))
        round_ = current_round(game)
        self.assertEqual(round_.question, question)
        [broadcast] = Broadcast.objects.all()
        self.assertEqual(broadcast.round, round_)
        self.assertEqual(broadcast.status, Broadcast.Status.PENDING)
        self.assertIn(broadcast, pending_broadcasts())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_worker_can_take_the_post_the_browser_recorded(self, roles):
        game = self._game()
        self._queue(game, self._question())
        self.client.post(self._url('game_next', game.pk))
        broadcast = Broadcast.objects.get()
        self.assertTrue(claim_broadcast(broadcast.pk))
        self.assertNotIn(broadcast, pending_broadcasts())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_reveals_the_round_in_play(self, roles):
        game = self._game()
        self._queue(game, self._question())
        self.client.post(self._url('game_next', game.pk))
        self.client.post(self._url('game_reveal', game.pk))
        round_ = current_round(game)
        self.assertTrue(round_.is_revealed)
        self.assertEqual(
            Broadcast.objects.get(kind=Broadcast.Kind.REVEAL).status,
            Broadcast.Status.PENDING)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_ends_the_quiz_and_its_scores_are_still_owed(self, roles):
        game = self._game()
        self.client.post(self._url('game_end', game.pk))
        game.refresh_from_db()
        self.assertEqual(game.state, Game.State.FINISHED)
        self.assertEqual(Broadcast.objects.get().kind, Broadcast.Kind.RECAP)
        self.assertIsNone(active_game(self.guild))

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_second_round_refuses_one_that_is_still_open(self, roles):
        game = self._game()
        self._queue(game, self._question())
        self.client.post(self._url('game_next', game.pk))
        response = self.client.post(self._url('game_next', game.pk), follow=True)
    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_control_room_queues_and_drops_questions(self, roles):
        game = self._game()
        question = self._question()
        self.client.post(self._url('game_queue', game.pk),
                         {'questions': [question.pk], 'quiz_type': ''})
        queued = game.rounds.get()
        self.assertEqual(queued.question, question)
        self.client.post(self._url('game_unqueue', game.pk),
                         {'rounds': [queued.pk]})
        self.assertEqual(game.rounds.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_clears_the_whole_queue(self, roles):
        game = self._game()
        self._queue(game, self._question())
        self.client.post(self._url('game_unqueue', game.pk), {'action': 'clear'})
        self.assertEqual(game.rounds.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_copies_the_questions_of_another_game(self, roles):
        question = self._question()
        past = self._game()
        self._queue(past, question)
        post_game_end(past, self.member)
        game = self._game()
        self.client.post(self._url('game_copy', game.pk),
                         {'source': past.pk, 'quiz_type': ''})
        self.assertEqual(game.rounds.get().question, question)

    def _running_round(self, variant: str = '', artist: str = '') -> Game:
        """Return a game with its first round open, and its question."""
        question = self._question(artist=artist)
        if variant:
            question.expected_answer.variants.create(text=variant)
        game = self._game()
        self._queue(game, question)
        self.client.post(self._url('game_next', game.pk))
        return game

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_control_room_lists_the_guesses_as_they_come(self, roles):
        game = self._running_round()
        submit_guess(current_round(game), self.guest, 'Wundervall', '')
        response = self.client.get(self._url('game_state', game.pk))
        self.assertContains(response, 'Guesses')
        self.assertContains(response, 'guestie')
        self.assertContains(response, 'Wundervall')
        self.assertContains(response, '<th>Team</th>')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_guesses_table_names_the_team_of_a_guess(self, roles):
        game = self._running_round()
        add_team(game, self.member, 'Reds', [self.guest])
        submit_guess(current_round(game), self.guest, 'Wundervall', '')
        response = self.client.get(self._url('game_state', game.pk))
        self.assertContains(response, '<th>Team</th>')
        self.assertContains(response, 'Reds')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_member_reads_no_other_guess(self, roles):
        game = self._running_round()
        submit_guess(current_round(game), self.host_player, 'Wundervall', '')
        self._as_guest()
        live = self.client.get(self._url('game_live', game.pk))
        self.assertNotContains(live, 'Wundervall')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_validates_a_guess_the_matcher_missed(self, roles):
        game = self._running_round()
        guess = submit_guess(current_round(game), self.guest, 'Wundervall', '')
        self.assertFalse(guess.text_correct)
        self.client.post(self._url('guess_correct', game.pk, guess.pk),
                         {'field': 'text', 'correct': '1'})
        guess.refresh_from_db()
        self.assertTrue(guess.text_correct)
        # The standings follow the flag on the next read.
        self.assertEqual(control_state(game)['scores'][0]['points'], 1)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_marks_a_matched_guess_wrong(self, roles):
        game = self._running_round()
        guess = submit_guess(current_round(game), self.guest, 'Wonderwall', '')
        self.assertTrue(guess.text_correct)
        self.client.post(self._url('guess_correct', game.pk, guess.pk),
                         {'field': 'text', 'correct': '0'})
        guess.refresh_from_db()
        self.assertFalse(guess.text_correct)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_corrects_a_guess_after_the_reveal(self, roles):
        game = self._running_round()
        guess = submit_guess(current_round(game), self.guest, 'Wundervall', '')
        self.client.post(self._url('game_reveal', game.pk))
        self.client.post(self._url('guess_correct', game.pk, guess.pk),
                         {'field': 'text', 'correct': '1'})
        guess.refresh_from_db()
        self.assertTrue(guess.text_correct)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_finished_game_refuses_the_correction(self, roles):
        game = self._running_round()
        guess = submit_guess(current_round(game), self.guest, 'Wundervall', '')
        self.client.post(self._url('game_end', game.pk))
        response = self.client.post(
            self._url('guess_correct', game.pk, guess.pk),
            {'field': 'text', 'correct': '1'}, follow=True)
        self.assertContains(response, 'This game is over.')
        guess.refresh_from_db()
        self.assertFalse(guess.text_correct)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_plain_member_corrects_nothing(self, roles):
        game = self._running_round()
        guess = submit_guess(current_round(game), self.guest, 'Wundervall', '')
        self._as_guest()
        response = self.client.post(
            self._url('guess_correct', game.pk, guess.pk),
            {'field': 'text', 'correct': '1'})
        self.assertEqual(response.status_code, 403)
        guess.refresh_from_db()
        self.assertFalse(guess.text_correct)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_guess_of_another_game_is_not_corrected(self, roles):
        game = self._running_round()
        admin = LocalMember(id=42, name='hostie',
                            guild_permissions=LocalPermissions(manage_guild=True))
        foreign = create_game(self._other_guild(), 555, admin)
        question = foreign.guild.questions.create(
            prompt='P',
            expected_answer=foreign.guild.answers.create(text='Other'))
        round_ = Round.objects.create(game=foreign, index=1, question=question,
                                      started_at=timezone.now())
        guess = Guess.objects.create(round=round_, text='Other',
                                     text_correct=True)
        response = self.client.post(
            self._url('guess_correct', game.pk, guess.pk),
            {'field': 'text', 'correct': '0'})
        self.assertEqual(response.status_code, 404)
        guess.refresh_from_db()
        self.assertTrue(guess.text_correct)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_member_guesses_the_round_from_the_browser(self, roles):
        game = self._running_round(artist='Oasis')
        self._as_guest()
        response = self.client.post(self._url('game_guess', game.pk),
                                    {'answer': 'Wonderwall',
                                     'secondary_answer': 'Oasis'})
        guess = guess_of(current_round(game), self.guest)
        self.assertTrue(guess.text_correct)
        self.assertTrue(guess.secondary_correct)
        self.assertContains(response, 'Wonderwall')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_guess_page_is_worded_like_the_modal(self, roles):
        game = self._running_round()
        self._as_guest()
        url = self._url('game_guess', game.pk)
        page = self.client.get(url)
        for text in (constants.GUESS_ANSWER_LABEL, constants.GUESS_ANSWER_HINT,
                     constants.GUESS_SECONDARY_LABEL,
                     constants.GUESS_SECONDARY_HINT):
            self.assertContains(page, str(text))
        # The recorded guess is labelled the same way once the form is gone.
        guessed = self.client.post(url, {'answer': 'Wonderwall',
                                         'secondary_answer': 'Oasis'})
        self.assertContains(guessed,
                            f'{constants.GUESS_SECONDARY_LABEL} “Oasis”')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_variant_counts_as_the_right_answer(self, roles):
        game = self._running_round(variant='Wonderwall (Live)')
        self._as_guest()
        self.client.post(self._url('game_guess', game.pk),
                         {'answer': 'Wonderwall (Live)',
                          'secondary_answer': ''})
        self.assertTrue(guess_of(current_round(game),
                                          self.guest).text_correct)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_wrong_guess_is_scored_wrong(self, roles):
        game = self._running_round()
        self._as_guest()
        self.client.post(self._url('game_guess', game.pk),
                         {'answer': 'Yesterday',
                          'secondary_answer': ''})
        self.assertFalse(guess_of(current_round(game),
                                           self.guest).text_correct)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_multiple_choice_round_is_guessed_with_a_pick(self, roles):
        question = self._choice_question()
        game = self._game(quiz_type=QuizType.MULTIPLE_CHOICE)
        self._queue(game, question)
        self.client.post(self._url('game_next', game.pk))
        self._as_guest()
        choice = question.choices.get(text='Wonderwall')
        response = self.client.post(self._url('game_guess', game.pk),
                                    {'choice': choice.pk,
                                     'secondary_answer': ''})
        self.assertTrue(guess_of(current_round(game),
                                          self.guest).text_correct)
        self.assertContains(response, 'You guessed')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_second_guess_is_refused(self, roles):
        game = self._running_round()
        self._as_guest()
        url = self._url('game_guess', game.pk)
        self.client.post(url, {'answer': 'One', 'secondary_answer': ''})
        response = self.client.post(url, {'answer': 'Other',
                                          'secondary_answer': ''},
                                    follow=True)
        self.assertContains(response, 'already guessed this round')
        self.assertEqual(Guess.objects.count(), 1)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_games_page_lists_the_games_of_the_server(self, roles):
        game = self._game()
        response = self.client.get(reverse('webadmin:games', args=[7]))
        self.assertContains(response, self._url('game', game.pk))
        self.assertContains(response, 'Set up a game')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_an_empty_guess_is_refused(self, roles):
        game = self._running_round()
        self._as_guest()
        response = self.client.post(self._url('game_guess', game.pk),
                                    {'answer': ' ',
                                     'secondary_answer': ''}, follow=True)
        self.assertContains(response, 'Give at least an answer.')
        self.assertEqual(Guess.objects.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_there_is_nothing_to_guess_before_a_round_opens(self, roles):
        game = self._game()
        self._as_guest()
        response = self.client.post(self._url('game_guess', game.pk),
                                    {'answer': 'Wonderwall',
                                     'secondary_answer': ''})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('webadmin:guild', args=[7]))
        self.assertEqual(Guess.objects.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_revealed_round_is_no_longer_guessed(self, roles):
        game = self._running_round()
        self.client.post(self._url('game_reveal', game.pk))
        self._as_guest()
        response = self.client.post(self._url('game_guess', game.pk),
                                    {'answer': 'Wonderwall',
                                     'secondary_answer': ''},
                                    follow=True)
        self.assertContains(response, 'No round is open to guess right now')
        self.assertEqual(Guess.objects.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_live_part_counts_the_guesses_the_browser_took(self, roles):
        game = self._running_round()
        self._as_guest()
        self.client.post(self._url('game_guess', game.pk),
                         {'answer': 'Wonderwall',
                          'secondary_answer': ''})
        state = control_state(game)
        self.assertEqual(state['round']['guesses']['guessed'], 1)
        # The count a host reads, and the one a player reads, are the same read.
        self.assertEqual(state['round']['guesses']['guessed'],
                         round_guesses(
                             current_round(game))['guessed'])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_plain_member_guesses_but_drives_nothing(self, roles):
        game = self._running_round()
        self._as_guest()
        self.assertEqual(self.client.get(self._url('game_guess', game.pk))
                         .status_code, 200)
        self.assertEqual(self.client.get(reverse('webadmin:games', args=[7]))
                         .status_code, 403)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_plain_member_polls_the_live_part_of_a_game(self, roles):
        game = self._running_round()
        self._as_guest()
        page = self.client.get(self._url('game_guess', game.pk))
        live_url = self._url('game_live', game.pk)
        self.assertContains(page, live_url)
        live = self.client.get(live_url)
        self.assertEqual(live.status_code, 200)
        self.assertContains(live, '0 guessed')
        # The host controls are not a member's to press.
        self.assertNotContains(live, 'Reveal the round')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_guess_given_in_discord_reaches_the_polling_page(self, roles):
        game = self._running_round()
        self._as_guest()
        live_url = self._url('game_live', game.pk)
        self.assertContains(self.client.get(live_url), '0 guessed')
        # The bot records the guess; the browser posted nothing.
        submit_guess(current_round(game), self.guest,
                              'Wonderwall', '')
        self.assertContains(self.client.get(live_url), '1 guessed')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_poll_asks_discord_nothing(self, roles):
        # A page polls every two seconds, so the roles must come from the
        # session rather than from a request to Discord on each one.
        game = self._running_round()
        with mock.patch('webadmin.discord_api.fetch_member_roles',
                        side_effect=AssertionError('Discord was asked')) as asked:
            self.client.get(self._url('game_live', game.pk))
            self.client.get(self._url('game_live', game.pk))
        self.assertEqual(asked.call_count, 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_control_room_polls_its_own_live_part(self, roles):
        game = self._running_round()
        page = self.client.get(self._url('game', game.pk))
        self.assertContains(page, self._url('game_state', game.pk))
        self.assertContains(self.client.get(self._url('game_state', game.pk)),
                            'Reveal the round')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_live_part_polls_only_while_the_page_is_visible(self, roles):
        # htmx re-arms its poll chain whatever the visibility: the filter is
        # what keeps a hidden tab from asking the server.
        game = self._running_round()
        for name in ('game', 'game_guess'):
            self.assertContains(
                self.client.get(self._url(name, game.pk)),
                'every 2s [!document.hidden]')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_stranger_drives_no_game(self, roles):
        game = self._game()
        self._as_guest()
        for name in ('game', 'game_state', 'game_publish', 'game_queue',
                     'game_unqueue', 'game_copy', 'game_next', 'game_reveal',
                     'game_end'):
            with self.subTest(route=name):
                response = self.client.post(self._url(name, game.pk), {})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(game.rounds.count(), 0)
        self.assertEqual(game.state, Game.State.RUNNING)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_server_the_player_is_not_in_is_not_reached(self, roles):
        game = self._running_round()
        session = self.client.session
        session[discord_api.GUILDS_SESSION_KEY] = []
        session.save()
        self.assertEqual(self.client.get(self._url('game', game.pk))
                         .status_code, 404)
        self.assertEqual(self.client.get(self._url('game_guess', game.pk))
                         .status_code, 404)
    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_control_room_renders_and_follows_the_game(self, roles):
        game = self._running_round()
        page = self.client.get(self._url('game', game.pk))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '0 guessed')
        state_url = self._url('game_state', game.pk)
        self.assertContains(page, state_url)
        self.assertContains(self.client.get(state_url), '0 guessed')
        # A host guessing their own round is what the live part follows.
        self.client.post(self._url('game_guess', game.pk),
                         {'answer': 'Wonderwall',
                          'secondary_answer': ''})
        state = control_state(game)
        self.assertEqual(state['round']['guesses']['guessed'], 1)
        self.assertContains(self.client.get(state_url), '1 guessed')

    def _field_id(self, response, form_id: str) -> str:
        """Return the signed id a picker page carries for its own search."""
        page = response.content.decode()
        match = re.search(rf'id="{form_id}"[^>]*?data-field_id="([^"]+)"',
                          page, re.S)
        if match is None:
            match = re.search(rf'data-field_id="([^"]+)"[^>]*?id="{form_id}"',
                              page, re.S)
        self.assertIsNotNone(match, f'{form_id} is not a search picker')
        return match.group(1)

    def _search(self, game, field_id: str, term: str = ''):
        """Ask the picker of a game for the options matching a term."""
        return self.client.get(self._url('game_search', game.pk),
                               {'field_id': field_id, 'term': term})

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_queue_picker_searches_the_questions_the_game_may_queue(self, roles):
        game = self._game()
        queued = self._question(text='Yesterday')
        wanted = self._question(text='Wonderwall')
        self._queue(game, queued)
        page = self.client.get(self._url('game', game.pk))
        results = self._search(game, self._field_id(page, 'queue_questions'),
                               'Wonderwall').json()['results']
        self.assertEqual([row['id'] for row in results], [wanted.pk])
        self.assertEqual(results[0]['text'], 'Guess it — answer: Wonderwall')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_search_never_offers_a_question_of_another_server(self, roles):
        game = self._game()
        other = Guild.objects.create(discord_id=8, name='Other Server')
        foreign = other.questions.create(
            prompt='Guess it',
            expected_answer=other.answers.create(text='Wonderwall'))
        page = self.client.get(self._url('game', game.pk))
        results = self._search(game, self._field_id(page, 'queue_questions'),
                               'Wonderwall').json()['results']
        self.assertNotIn(foreign.pk, [row['id'] for row in results])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_search_offers_no_question_the_game_already_queued(self, roles):
        game = self._game()
        question = self._question()
        self._queue(game, question)
        page = self.client.get(self._url('game', game.pk))
        results = self._search(game, self._field_id(page, 'queue_questions'),
                               'Wonderwall').json()['results']
        self.assertEqual(results, [])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_copy_picker_searches_the_past_games_of_the_server(self, roles):
        past = self._game()
        past.name = 'Friday quiz'
        past.save(update_fields=['name'])
        post_game_end(past, self.member)
        game = self._game()
        page = self.client.get(self._url('game', game.pk))
        results = self._search(game, self._field_id(page, 'copy_source'),
                               'Friday').json()['results']
        self.assertEqual([row['id'] for row in results], [past.pk])
        self.assertIn('Friday quiz', results[0]['text'])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_search_is_opened_by_the_members_of_the_server_only(self, roles):
        game = self._game()
        page = self.client.get(self._url('game', game.pk))
        field_id = self._field_id(page, 'queue_questions')
        self._as_guest()
        self.assertEqual(self._search(game, field_id, 'Wonderwall').status_code,
                         200)
        session = self.client.session
        session[discord_api.GUILDS_SESSION_KEY] = []
        session.save()
        self.assertEqual(self._search(game, field_id, 'Wonderwall').status_code,
                         404)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_control_room_loads_the_assets_of_its_pickers(self, roles):
        game = self._game()
        page = self.client.get(self._url('game', game.pk))
        self.assertContains(page, 'django_select2/django_select2.js')
        self.assertContains(page, self._url('game_search', game.pk))

    def test_every_picker_stretches_to_the_width_of_its_column(self):
        # A select2 widget otherwise renders narrower than the crispy column.
        for widget in (forms.SearchWidget(), forms.QuestionWidget(),
                       forms.GameWidget()):
            with self.subTest(widget=type(widget).__name__):
                self.assertEqual(widget.attrs['style'], 'width : 100%')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_question_past_the_first_ones_can_be_queued(self, roles):
        game = self._game()
        last = self._question(text='Zebra')
        for index in range(constants.MAX_CHOICES):
            self._question(text=f'Filler {index}')
        page = self.client.get(self._url('game', game.pk))
        self._search(game, self._field_id(page, 'queue_questions'), 'Zebra')
        self.client.post(self._url('game_queue', game.pk),
                         {'questions': [last.pk], 'quiz_type': ''})
        self.assertEqual(game.rounds.get().question, last)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_live_part_reads_little(self, roles):
        # The page polls every two seconds, so its read must stay small.
        game = self._running_round()
        self.client.get(self._url('game_state', game.pk))
        with self.assertNumQueries(15):
            self.client.get(self._url('game_state', game.pk))

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_control_room_lists_the_broadcasts_of_its_game(self, roles):
        game = self._running_round()
        sent = enqueue(game, Broadcast.Kind.PUBLISH)
        mark_broadcast_sent(sent, [11])
        mark_broadcast_failed(enqueue(game, Broadcast.Kind.RECAP),
                              'its channel is gone')
        response = self.client.get(self._url('game_state', game.pk))
        self.assertContains(response, 'Recent broadcasts')
        # A post that went out is listed too: this is not only a stuck list.
        self.assertContains(response, sent.get_status_display())
        self.assertContains(response, 'its channel is gone')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_control_room_shows_no_post_of_another_game(self, roles):
        game = self._running_round()
        admin = LocalMember(id=42, name='hostie',
                            guild_permissions=LocalPermissions(manage_guild=True))
        foreign = create_game(self._other_guild(), 555, admin)
        mark_broadcast_failed(enqueue(foreign, Broadcast.Kind.PUBLISH),
                              'a post of another game')
        response = self.client.get(self._url('game_state', game.pk))
        self.assertNotContains(response, 'a post of another game')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_member_is_not_offered_the_posts_of_a_game(self, roles):
        game = self._running_round()
        enqueue(game, Broadcast.Kind.PUBLISH)
        self._as_guest()
        response = self.client.get(self._url('game_guess', game.pk))
        self.assertNotContains(response, 'Recent broadcasts')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value=set())
    def test_the_server_page_lists_the_broadcasts_that_did_not_go_out(self, bot_guilds,
                                                                roles):
        game = self._game()
        broadcast = enqueue(game, Broadcast.Kind.PUBLISH)
        mark_broadcast_failed(broadcast, 'its channel is gone')
        response = self.client.get(self._url('guild'))
        self.assertContains(response, 'Broadcasts that have not gone out')
        self.assertContains(response, 'its channel is gone')
        # A post still waiting its turn is not offered for a manual try.
        self.assertNotContains(
            response,
            reverse('webadmin:broadcast_retry',
                    args=[self.guild.discord_id, broadcast.pk]))

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value=set())
    def test_the_dashboard_no_longer_shows_them(self, bot_guilds, roles):
        game = self._game()
        mark_broadcast_failed(enqueue(game, Broadcast.Kind.PUBLISH),
                              'its channel is gone')
        response = self.client.get(reverse('webadmin:dashboard'))
        self.assertNotContains(response, 'Broadcasts that have not gone out')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value=set())
    def test_a_dead_post_can_be_retried_from_the_server_page(self, bot_guilds,
                                                             roles):
        game = self._game()
        broadcast = enqueue(game, Broadcast.Kind.PUBLISH)
        for _ in range(constants.BROADCAST_MAX_ATTEMPTS):
            mark_broadcast_failed(broadcast, 'its channel is gone')
        response = self.client.post(
            self._url('broadcast_retry', broadcast.pk), follow=True)
        self.assertContains(response, 'The post is queued again.')
        broadcast.refresh_from_db()
        self.assertEqual(broadcast.status, Broadcast.Status.PENDING)
        self.assertEqual(broadcast.attempts, 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value=set())
    def test_a_post_of_another_server_is_not_retried(self, bot_guilds, roles):
        admin = LocalMember(id=42, name='hostie',
                            guild_permissions=LocalPermissions(manage_guild=True))
        foreign = create_game(self._other_guild(), 555, admin)
        broadcast = enqueue(foreign, Broadcast.Kind.PUBLISH)
        mark_broadcast_failed(broadcast, 'its channel is gone')
        response = self.client.post(
            self._url('broadcast_retry', broadcast.pk), follow=True)
        self.assertContains(response, 'That post is gone.')
        broadcast.refresh_from_db()
        self.assertEqual(broadcast.status, Broadcast.Status.FAILED)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    @mock.patch('webadmin.discord_api.fetch_bot_guild_ids', return_value=set())
    def test_only_a_host_may_retry_a_post(self, bot_guilds, roles):
        broadcast = enqueue(self._game(), Broadcast.Kind.PUBLISH)
        self._as_guest()
        response = self.client.post(self._url('broadcast_retry', broadcast.pk))
        self.assertEqual(response.status_code, 403)
        broadcast.refresh_from_db()
        self.assertEqual(broadcast.status, Broadcast.Status.PENDING)

    def test_the_recent_posts_of_a_game_are_listed_newest_first(self):
        game = self._game()
        first = enqueue(game, Broadcast.Kind.PUBLISH)
        second = enqueue(game, Broadcast.Kind.RECAP)
        mark_broadcast_sent(second, [11])
        rows = game_broadcasts(game)
        self.assertEqual([row['broadcast'] for row in rows], [second.pk, first.pk])
        self.assertEqual(rows[0]['status'], Broadcast.Status.SENT.label)
        self.assertFalse(rows[0]['dead'])

    def test_the_recent_posts_of_a_game_are_capped(self):
        game = self._game()
        for _ in range(constants.BROADCAST_PANEL_SIZE + 3):
            enqueue(game, Broadcast.Kind.PUBLISH)
        self.assertEqual(len(game_broadcasts(game)),
                         constants.BROADCAST_PANEL_SIZE)

    def test_a_sent_post_is_not_in_the_unfinished_list(self):
        game = self._game()
        broadcast = enqueue(game, Broadcast.Kind.PUBLISH)
        mark_broadcast_sent(broadcast, [11])
        self.assertEqual(unfinished_broadcasts(self.guild), [])
        self.assertEqual([row['broadcast'] for row in game_broadcasts(game)],
                         [broadcast.pk])

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_revealed_game_shows_its_standings(self, roles):
        game = self._running_round()
        self.client.post(self._url('game_guess', game.pk),
                         {'answer': 'Wonderwall',
                          'secondary_answer': ''})
        self.client.post(self._url('game_end', game.pk))
        response = self.client.get(self._url('game', game.pk))
        self.assertContains(response, 'Standings')
        self.assertContains(response, 'hostie')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_the_standings_rank_a_team_with_its_players_points(self, roles):
        game = self._running_round()
        add_team(game, self.member, 'Reds', [self.guest])
        submit_guess(current_round(game), self.guest, 'Wundervall', '')
        response = self.client.get(self._url('game_state', game.pk))
        self.assertContains(response, 'Reds')
        self.assertContains(response, 'Team')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_game_cannot_be_set_up_while_another_one_runs(self, roles):
        self._game()
        response = self.client.post(self._url('game_setup'),
                                    self._setup_data(), follow=True)
        self.assertContains(response, 'already running')
        self.assertEqual(Game.objects.count(), 1)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_setup_without_a_type_is_refused(self, roles):
        response = self.client.post(self._url('game_setup'),
                                    self._setup_data(quiz_type=''), follow=True)
        self.assertContains(response, 'This field is required.')
        self.assertEqual(Game.objects.count(), 0)

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_game_of_another_server_is_not_found(self, roles):
        other = Guild.objects.create(discord_id=8, name='Other Server')
        admin = LocalMember(id=42, name='hostie',
                            guild_permissions=LocalPermissions(manage_guild=True))
        foreign = create_game(other, 555, admin)
        self.assertEqual(self.client.get(self._url('game', foreign.pk))
                         .status_code, 404)
