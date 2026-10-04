"""Tests for the web admin's Discord login and landing pages."""

import re
from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from blindtest.models import Question

from blindtest import services
from discordcore.members import LocalMember, LocalPermissions, LocalRole
from discordcore.models import Guild, Host, Player

from . import discord_api

ACCOUNT = {'id': '42', 'username': 'hostie'}
GUILDS = [{'id': '7', 'name': 'Server Seven',
           'permissions': str(discord_api.MANAGE_GUILD)}]
IDENTITY = {'account': ACCOUNT, 'guilds': GUILDS}

# The member Discord reports for the host of the server the tests set up.
WENDY = {'user': {'id': '42', 'username': 'wendy'}, 'nick': 'Wendy'}


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
    def test_a_host_drops_every_variant_of_a_question(self, roles):
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
        self.assertContains(response, 'Drop the question')
        self.assertContains(response, 'Drop the answer')

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_drops_an_unused_question(self, roles):
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Mine'))
        self.client.post(
            reverse('webadmin:question_drop', args=[7, question.pk]))
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_host_drops_an_unused_answer(self, roles):
        answer = self.guild.answers.create(text='Spare')
        self.client.post(reverse('webadmin:answer_drop', args=[7, answer.pk]))
        self.assertFalse(self.guild.answers.filter(pk=answer.pk).exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_dropping_an_answer_a_question_uses_reports_why(self, roles):
        answer = self.guild.answers.create(text='Mine')
        self.guild.questions.create(prompt='Guess it', expected_answer=answer)
        response = self.client.post(
            reverse('webadmin:answer_drop', args=[7, answer.pk]), follow=True)
        self.assertContains(response, 'used by the question')
        self.assertTrue(self.guild.answers.filter(pk=answer.pk).exists())

    @mock.patch('webadmin.discord_api.fetch_member_roles', return_value=[])
    def test_a_stranger_drops_nothing(self, roles):
        self._as_plain_member()
        question = self.guild.questions.create(
            expected_answer=self.guild.answers.create(text='Mine'))
        self.client.post(
            reverse('webadmin:question_drop', args=[7, question.pk]))
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
