"""Tests for the Discord layer: cog loading, commands and the ORM bridge."""

import asyncio
import ast
from collections.abc import Iterable
from inspect import getsource

import discord
from discord import InteractionType
from discord.ext import commands
from django.core.cache import cache
from django.db import connections
from django.test import SimpleTestCase, TransactionTestCase

from discordblindtest.testing import NoNetworkMixin
from blindtest import constants, services
from blindtest.models import (Answer, Broadcast, Game, Guess, Question, QuizType,
                              Round)
from discordcore.mentions import user_mention
from discordcore.models import Guild, Player
from blindtest.services.broadcasts import (claim_broadcast, post_round_reveal,
                                           post_round_open)
from blindtest.services.games import active_game, create_game, end_game
from blindtest.services.guessing import submit_guess
from blindtest.services.guilds import (add_host, require_host,
                                       set_default_channel,
                                       set_default_ping_role)
from blindtest.services.library import (add_question, edit_question,
                                        question_choices)
from blindtest.services.rounds import (create_round, current_round,
                                       queued_count, reveal_round, open_round)
from blindtest.services.teams import add_team

from . import embeds
from .bot import create_bot
from .cogs.admin import AdminCog
from .cogs.game import GameCog, answer_form_title
from .cogs.library import LibraryCog, question_autocomplete
from .cogs.teams import (NO_GAME, TeamsCog,
                         copyable_team_autocomplete, team_autocomplete)
from .db import guild_for, player_for, run_db
from .ui import (ANSWER_TITLE, HOST_END_ID, SETUP_ADD_ID, SETUP_CLEAR_ID,
                 SETUP_COPY_ID, SETUP_END_ID, SETUP_PUBLISH_ID,
                 SETUP_REMOVE_ID, GuessModal, HostPanel, SetupPanel)


def game_payload(**overrides) -> dict:
    """Return a payload shaped like the ones the services return."""
    payload = {'game_name': 'Fiesta', 'type_label': 'Blind test',
               'type': 'BLIND_TEST', 'scoring_label': 'standard (fixed points)',
               'index': 3, 'prompt': 'Guess it', 'media_url': '',
               'question_text': 'Song (Band)', 'host_text': 'Song (Band)',
               'answer_text': 'Song (Band)', 'expected': 'Song', 'options': [],
               'prompt_set': True,
               'queued': 7, 'answered': 2, 'right': 1, 'right_names': ['user43'],
               'player_scores': [], 'team_scores': [], 'rounds': 1, 'answers': 2,
               'available': 5, 'created_at': None, 'finished_at': None,
               'game_id': 1, 'channel_id': 100}
    payload.update(overrides)
    return payload


def embed_text(embed: discord.Embed) -> str:
    """Return every text an embed displays."""
    parts = [embed.title or '', embed.description or '']
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    parts.extend(field.name + field.value for field in embed.fields)
    return ' '.join(parts)


def answer_form(index: int = 1, quiz_type: str = 'BLIND_TEST',
                options: list[dict] | None = None) -> dict:
    """Return an answer form payload, as the services build it."""
    return {'round_id': 1, 'index': index, 'type': quiz_type,
            'options': options or []}


class EmbedTests(NoNetworkMixin, SimpleTestCase):
    """Public embeds name the game and stay within Discord's limits."""

    def test_every_embed_names_the_game(self) -> None:
        scores = [{'username': 'a', 'discord_name': 'A', 'points': 3}]
        payload = game_payload(player_scores=scores)
        built = [embeds.publication_embed(payload, '<@1>'),
                 embeds.round_embed(payload),
                 embeds.reveal_embed(payload),
                 embeds.scores_embed(
                     payload, 'Round 3 scores',
                     embeds.leader_line(scores, 'is currently winning')),
                 embeds.recap_embed(payload)]
        for embed in built:
            with self.subTest(embed=embed.title):
                self.assertIn('Fiesta', embed.title)

    def test_the_publication_shows_no_host_command(self) -> None:
        embed = embeds.publication_embed(game_payload(), '<@1>')
        text = embed_text(embed)
        for command in ('/quiz', '/blindtest'):
            with self.subTest(command=command):
                self.assertNotIn(command, text)
        self.assertTrue(embed.footer.text is None)

    def test_no_public_embed_leaks_the_answer_before_the_reveal(self) -> None:
        payload = game_payload(prompt='Guess the song!', question_text='Guess the song!',
                               answer_text='Zebra', expected='Zebra')
        for embed in (embeds.publication_embed(payload, '<@1>'),
                      embeds.round_embed(payload)):
            with self.subTest(embed=embed.title):
                self.assertNotIn('Zebra', embed_text(embed))

    def test_the_round_scores_share_the_shape_of_the_final_scores(self) -> None:
        scores = [{'username': 'a', 'discord_name': 'A', 'points': 3}]
        payload = game_payload(player_scores=scores,
                               team_scores=[{'name': 'Reds', 'points': 3}])
        round_scores = embeds.scores_embed(
            payload, 'Round 3 scores',
            embeds.leader_line(scores, 'is currently winning'))
        final = embeds.recap_embed(payload)
        self.assertEqual(round_scores.fields[0].name, 'Standings')
        self.assertEqual(final.fields[0].name, 'Standings')
        self.assertEqual(round_scores.footer.text, final.footer.text)
        self.assertTrue(round_scores.title.endswith('scores'))
        self.assertTrue(final.title.endswith('scores'))
        self.assertEqual(round_scores.description, '🥇 A is currently winning!')
        self.assertEqual(final.description, '🥇 A wins!')

    def test_the_round_embed_shows_the_type_and_the_queue(self) -> None:
        embed = embeds.round_embed(game_payload(queued=4))
        self.assertIn('**Guess it**', embed.description)
        self.assertEqual(embed.fields[0].value, '4 question(s) queued')
        self.assertIn('Blind test', embed.fields[0].name)

    def test_the_round_embed_never_links_to_the_media(self) -> None:
        embed = embeds.round_embed(
            game_payload(media_url='https://youtu.be/1'))
        self.assertNotIn('Listen', embed_text(embed))

    def test_the_reveal_links_to_the_media_of_the_question(self) -> None:
        embed = embeds.reveal_embed(
            game_payload(media_url='https://youtu.be/1'))
        self.assertIn('[🎧 Listen](https://youtu.be/1)', embed.description)
        self.assertNotIn('Listen',
                         embed_text(embeds.reveal_embed(game_payload())))

    def test_the_reveal_marks_the_right_multiple_choice_option(self) -> None:
        embed = embeds.reveal_embed(game_payload(
            type='MULTIPLE_CHOICE',
            options=[{'pk': 1, 'label': 'Song'}, {'pk': 2, 'label': 'Other'}]))
        self.assertIn('✅ Song', embed.description)
        self.assertIn('❌ Other', embed.description)
        self.assertIn('1 of 2', embed.fields[0].value)
        self.assertEqual(embed.fields[1].name, 'Correct players')
        self.assertIn('user43', embed.fields[1].value)

    def test_the_recap_summarises_the_game(self) -> None:
        embed = embeds.recap_embed(game_payload(
            player_scores=[{'username': 'a', 'discord_name': 'A', 'points': 3}],
            team_scores=[{'name': 'Reds', 'points': 3}], rounds=3, answers=9))
        names = [field.name for field in embed.fields]
        self.assertEqual(names, ['Standings', 'Teams', 'Rounds played',
                                 'Answers given', 'Quiz type'])
        self.assertEqual(embed.description, '🥇 A wins!')

    def test_a_long_standings_list_says_how_many_players_are_left(self) -> None:
        scores = [{'username': f'user{index}', 'discord_name': 'n' * 200,
                   'points': index} for index in range(40)]
        embed = embeds.recap_embed(game_payload(player_scores=scores))
        standings = embed.fields[0]
        self.assertEqual(standings.name, 'Standings')
        self.assertLessEqual(len(standings.value), embeds.FIELD_VALUE_LIMIT)
        self.assertIn('more players', standings.value)

    def test_an_oversized_embed_is_trimmed_instead_of_failing(self) -> None:
        payload = game_payload(prompt='p' * 5000, answer_text='a' * 5000)
        for embed in (embeds.round_embed(payload), embeds.reveal_embed(payload)):
            with self.subTest(embed=embed.title):
                embeds.fit(embed)
                self.assertLessEqual(len(embed.description),
                                     embeds.DESCRIPTION_LIMIT)

    def test_fields_are_capped_and_clipped(self) -> None:
        embed = discord.Embed(title='t')
        for index in range(40):
            embed.add_field(name='n' * 500, value='v' * 2000)
        embeds.fit(embed)
        self.assertLessEqual(len(embed.fields), embeds.FIELD_LIMIT)
        for field in embed.fields:
            self.assertLessEqual(len(field.name), embeds.FIELD_NAME_LIMIT)
            self.assertLessEqual(len(field.value), embeds.FIELD_VALUE_LIMIT)

    def test_a_message_stays_under_the_total_limit(self) -> None:
        scores = [{'username': f'user{index}', 'discord_name': 'n' * 200,
                   'points': index} for index in range(40)]
        payload = game_payload(player_scores=scores)
        built = embeds.fit_all([embeds.reveal_embed(payload),
                                embeds.scores_embed(
                                    payload, 'Round 3 scores',
                                    embeds.leader_line(scores, 'wins')),
                                embeds.recap_embed(payload)])
        self.assertLessEqual(sum(embeds.size(embed) for embed in built),
                             embeds.TOTAL_LIMIT)

    def test_post_clips_the_content(self) -> None:
        channel = FakeChannel()
        asyncio.run(embeds.post(channel, content='x' * 5000))
        [content] = channel.contents
        self.assertEqual(len(content), embeds.CONTENT_LIMIT)


class FakeRole:
    """Minimal stand-in for a ``discord.Role``."""

    def __init__(self, role_id: int, guild=None, position: int = 1) -> None:
        self.id = role_id
        self.guild = guild
        self.position = position
        self.mention = f'<@&{role_id}>'


class FakePermissions:
    """Minimal stand-in for ``discord.Permissions``."""

    def __init__(self, manage_guild: bool = False) -> None:
        self.manage_guild = manage_guild


class FakeMember:
    """Minimal stand-in for a ``discord.Member``."""

    def __init__(self, user_id: int, roles: Iterable[int] = (),
                 manage_guild: bool = False, top_role: FakeRole | None = None,
                 nickname: str = '') -> None:
        self.id = user_id
        self.name = f'user{user_id}'
        self.nickname = nickname
        self.roles = [FakeRole(role_id) for role_id in roles]
        self.guild_permissions = FakePermissions(manage_guild)
        self.top_role = top_role or FakeRole(0, position=0)

    @property
    def display_name(self) -> str:
        return self.nickname or self.name

    @property
    def mention(self) -> str:
        return f'<@{self.id}>'


class FakeGuild:
    """Minimal stand-in for a ``discord.Guild``."""

    def __init__(self, guild_id: int, name: str = 'Server') -> None:
        self.id = guild_id
        self.name = name
        self.channels = {}
        # The bot sits above every role a test may want to call in.
        self.me = FakeMember(1, top_role=FakeRole(999, self, position=50))

    def get_channel(self, channel_id: int):
        """Return a channel the guild holds, when it holds one."""
        return self.channels.get(channel_id)

    def role(self, role_id: int, position: int = 1) -> FakeRole:
        """Return a role of this guild, below the bot."""
        return FakeRole(role_id, self, position)


class FakeGuildChannel:
    """Minimal stand-in for a channel of a ``discord.Guild``."""

    def __init__(self, guild: FakeGuild, channel_id: int,
                 name: str = 'quiz') -> None:
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.mention = f'<#{channel_id}>'

    async def send(self, content=None, *, embed=None, embeds=(), view=None):
        """Record nothing: only the identity of the channel is under test."""
        return FakeMessage()


class FakeChannel:
    """Records the messages a flow posted in a channel."""

    def __init__(self, channel_id: int = 100, name: str = 'lounge') -> None:
        self.id = channel_id
        self.name = name
        self.mention = f'<#{channel_id}>'
        self.embeds = []
        self.contents = []
        self.messages = []
        self.allowed_mentions = []

    async def send(self, content=None, *, embed=None, embeds=(), view=None,
                   allowed_mentions=None):
        message = FakeMessage(500 + len(self.messages))
        self.contents.append(content)
        self.embeds.extend(embeds or ([embed] if embed else []))
        self.messages.append(message)
        self.allowed_mentions.append(allowed_mentions)
        return message


class FakeClient:
    """Stand-in for a client holding no channel in its cache."""

    def get_channel(self, channel_id: int):
        """Report the channel as not cached."""
        return None


class FakeMessage:
    """A message that must never be edited through the channel endpoint."""

    def __init__(self, message_id: int = 500) -> None:
        self.id = message_id
        self.jump_url = f'https://discord.com/channels/1/{message_id}'

    async def edit(self, **kwargs) -> None:
        """Fail, as an ephemeral message is not editable this way."""
        raise AssertionError('edited a message the channel endpoint cannot see')


class FakeResponse:
    """Records how a flow answered, refusing a second answer."""

    def __init__(self) -> None:
        self.deferred = []
        self.edits = []
        self.modals = []
        self.messages = []
        self.answered = False

    async def defer(self, **kwargs) -> None:
        self.claim()
        self.deferred.append(kwargs)

    async def edit_message(self, *, view=None) -> None:
        self.claim()
        self.edits.append(view)

    async def send_message(self, content: str, **kwargs) -> None:
        self.claim()
        self.messages.append(content)

    async def send_modal(self, modal) -> None:
        self.claim()
        self.modals.append(modal)

    def claim(self) -> None:
        """Reject a second answer, as Discord does."""
        if self.answered:
            raise AssertionError('answered an interaction twice')
        self.answered = True


class FakeFollowup:
    """Records the private messages a flow sent."""

    def __init__(self) -> None:
        self.sent = []
        self.views = []

    async def send(self, content=None, **kwargs) -> None:
        self.sent.append(content)
        self.views.append(kwargs.get('view'))


class FakeInteraction:
    """Minimal stand-in for a guild ``discord.Interaction``."""

    def __init__(self,
                 kind: InteractionType | None = None) -> None:
        self.type = kind or InteractionType.application_command
        self.guild = FakeGuild(1)
        self.user = FakeMember(42)
        self.channel = FakeChannel()
        self.client = FakeClient()
        self.channel_id = 100
        self.message = (FakeMessage()
                        if self.type is InteractionType.component else None)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.original_edits = []
        self.original_contents = []

    async def edit_original_response(self, *, view=None, content=None) -> None:
        """Record the update that completes a deferred message update."""
        if not self.response.deferred:
            raise AssertionError('updated a response that was never deferred')
        self.original_edits.append(view)
        self.original_contents.append(content)


async def _slash_command_names() -> list[str]:
    bot = create_bot()
    await bot.load_cogs()
    return sorted(command.qualified_name for command in bot.tree.walk_commands())


async def _question_options(name: str) -> tuple[str, ...]:
    """Return the options a ``/library question`` subcommand takes."""
    bot = create_bot()
    await bot.load_cogs()
    command = next(command for command in bot.tree.walk_commands()
                   if command.qualified_name == f'library question {name}')
    return tuple(parameter.name for parameter in command.parameters)


async def _loaded_bot() -> commands.Bot:
    """Return a bot with its cogs loaded, the way ``setup_hook`` does it."""
    bot = create_bot()
    await bot.load_cogs()
    return bot


class BotSetupTests(NoNetworkMixin, SimpleTestCase):
    def test_the_cog_registers_the_documented_commands(self) -> None:
        names = asyncio.run(_slash_command_names())
        self.assertEqual(names, [
            'admin', 'admin channel', 'admin channel clear',
            'admin channel set', 'admin channel show', 'admin host',
            'admin host add', 'admin host list',
            'admin host remove', 'admin ping', 'admin ping clear',
            'admin ping set', 'admin ping show', 'blindtest',
            'blindtest clear', 'blindtest copy', 'blindtest end',
            'blindtest guess', 'blindtest next', 'blindtest panel',
            'blindtest publish', 'blindtest queue', 'blindtest reveal',
            'blindtest setup', 'blindtest unqueue', 'library',
            'library answer', 'library answer add', 'library question',
            'library question add', 'library question edit',
            'library variant', 'library variant add',
            'library variant list', 'library variant remove', 'ping',
            'quiz', 'quiz clear', 'quiz copy', 'quiz end', 'quiz guess',
            'quiz next', 'quiz panel', 'quiz publish', 'quiz queue',
            'quiz reveal', 'quiz setup', 'quiz unqueue',
            'teams', 'teams add', 'teams copy', 'teams list', 'teams members',
            'teams members add', 'teams members remove',
            'teams remove', 'teams rename',
        ])


class GameControlTests(NoNetworkMixin, SimpleTestCase):
    def test_the_controls_survive_a_bot_restart(self) -> None:
        views = asyncio.run(_loaded_bot()).get_cog('GameCog').controls()
        self.assertTrue(all(view.timeout is None for view in views))
        self.assertTrue(all(view.is_persistent() for view in views))
        self.assertEqual(
            sorted(item.custom_id for view in views for item in view.children),
            ['blindtest_host_end', 'blindtest_host_next',
             'blindtest_host_queue', 'blindtest_host_reveal',
             'blindtest_player_answer', 'blindtest_queue_pick',
             'blindtest_setup_add', 'blindtest_setup_clear',
             'blindtest_setup_copy', 'blindtest_setup_end',
             'blindtest_setup_publish', 'blindtest_setup_remove'])

    def test_the_cog_registers_its_controls_when_it_loads(self) -> None:
        bot = asyncio.run(_loaded_bot())
        self.assertEqual(
            sorted({type(view).__name__ for view in bot.persistent_views}),
            ['GamePanel', 'HostPanel', 'QueuePanel', 'SetupPanel'])

    def test_the_controls_are_wired_to_the_cog(self) -> None:
        cog = asyncio.run(_loaded_bot()).get_cog('GameCog')
        for view in cog.controls():
            with self.subTest(view=type(view).__name__):
                self.assertIs(view.cog, cog)
        for operation in ('ask_question', 'clear', 'copy_questions',
                          'copy_selection', 'remove_selection', 'end_game',
                          'open_guess_form', 'open_next_round', 'publish',
                          'queue_question', 'queue_selection', 'record_guess',
                          'reveal_round', 'send_setup_panel',
                          'unqueue_question'):
            with self.subTest(operation=operation):
                self.assertTrue(callable(getattr(cog, operation, None)))


class OrmBridgeTests(NoNetworkMixin, SimpleTestCase):
    def test_the_discord_layer_never_touches_the_orm(self) -> None:
        """Only the bridge may build ORM calls, so no lazy row can reach the loop."""
        from . import ui
        from .cogs import admin, game, library, teams
        for module in (admin, game, library, teams, ui):
            with self.subTest(module=module.__name__):
                self.assertNotIn('.objects.', getsource(module))
        with self.subTest(module=embeds.__name__):
            self.assertNotIn('.objects.', getsource(embeds))


class RunDbTests(NoNetworkMixin, TransactionTestCase):
    def setUp(self) -> None:
        # A cached row outlives a test and would point at a rolled back one.
        cache.clear()

    def tearDown(self) -> None:
        # The ORM thread owns its own connection; close it so the test
        # database can be dropped at the end of the run.
        asyncio.run(run_db(connections.close_all))

    def test_run_db_returns_plain_values(self) -> None:
        Answer.objects.create(text='Song')
        self.assertEqual(asyncio.run(run_db(Answer.objects.count)), 1)

    def test_values_are_evaluated_in_the_orm_thread(self) -> None:
        Answer.objects.create(text='Song')
        texts = asyncio.run(run_db(
            lambda: list(Answer.objects.values_list('text', flat=True))))
        self.assertEqual(texts, ['Song'])

    def test_guild_for_creates_the_guild_row(self) -> None:
        guild = asyncio.run(guild_for(FakeInteraction()))
        self.assertEqual(guild.discord_id, 1)
        self.assertEqual(guild.name, 'Server')
        self.assertEqual(Guild.objects.count(), 1)
        self.assertEqual(Player.objects.count(), 0)

    def test_player_for_creates_the_player_row(self) -> None:
        player = asyncio.run(player_for(FakeMember(42)))
        self.assertEqual(player.discord_user_id, 42)
        self.assertEqual(player.discord_name, 'user42')
        self.assertEqual(Player.objects.count(), 1)

    def test_domain_errors_cross_the_bridge(self) -> None:
        guild = Guild.objects.create(discord_id=1, name='Server')
        with self.assertRaises(PermissionError):
            asyncio.run(run_db(require_host, guild, FakeMember(43)))


class FlowResultTests(NoNetworkMixin, TransactionTestCase):
    def setUp(self) -> None:
        cache.clear()

    def tearDown(self) -> None:
        asyncio.run(run_db(connections.close_all))

    def _revealed_payload(self) -> dict:
        def build() -> dict:
            guild = Guild.objects.create(discord_id=1, name='Server')
            host = FakeMember(42)
            add_host(
                guild, user_mention(host.id), FakeMember(42, manage_guild=True))
            game = create_game(guild, 100, host)
            question = Question.objects.create(
                expected_answer=Answer.objects.create(text='Song'),
                secondary_answer=Answer.objects.create(text='Band'))
            round_ = Round.objects.get(
                pk=open_round(game, host, question)['round_id'])
            submit_guess(
                round_, Player.objects.from_discord(FakeMember(43)),
                'Song', 'Band')
            return reveal_round(round_, host)
        return asyncio.run(run_db(build))

    def test_the_reveal_crosses_the_bridge_without_lazy_queries(self) -> None:
        result = self._revealed_payload()
        self.assertEqual(result['answer_text'], 'Song (Band)')
        self.assertEqual([row['points'] for row in result['player_scores']], [2])

    @staticmethod
    def _picker_choices() -> list[dict]:
        """Build a game with one unplayed question and return its picker choices."""
        guild = Guild.objects.create(discord_id=1, name='Server')
        host = FakeMember(42)
        add_host(
            guild, user_mention(host.id), FakeMember(42, manage_guild=True))
        game = create_game(guild, 100, host)
        Question.objects.create(
            expected_answer=Answer.objects.create(text='Song'),
            secondary_answer=Answer.objects.create(text='Band'))
        return question_choices(game)

    def test_picker_choices_cross_the_bridge_without_lazy_queries(self) -> None:
        async def check() -> None:
            # Assertions run on the event loop: a lazily loaded row would raise
            # SynchronousOnlyOperation here, the way the reveal used to.
            [choice] = await run_db(self._picker_choices)
            self.assertEqual(choice['label'], 'Song (Band)')
            self.assertIsInstance(choice['pk'], int)
        asyncio.run(check())


class FlowTestCase(NoNetworkMixin, TransactionTestCase):
    """A guild with a host, a running game and a round in play."""

    def setUp(self) -> None:
        cache.clear()

    def tearDown(self) -> None:
        asyncio.run(run_db(connections.close_all))

    @staticmethod
    def question(quiz_type: str, text: str, secondary: str) -> Question:
        """Return a question the quiz type can play."""
        expected = Answer.objects.create(text=text)
        question = Question.objects.create(
            prompt=f'Which song? ({text})' if quiz_type else '',
            expected_answer=expected,
            secondary_answer=Answer.objects.create(text=secondary))
        if quiz_type == QuizType.MULTIPLE_CHOICE:
            question.choices.set([expected,
                                  Answer.objects.create(text=f'No {text}')])
        return question

    def create_game(self, quiz_type: str = '',
                     ping_role_id: int | None = None) -> None:
        """Create the rows a flow works on, as the bot would have."""
        guild = Guild.objects.create(discord_id=1, name='Server')
        add_host(guild, user_mention(42),
                          FakeMember(42, manage_guild=True))
        game = create_game(guild, 100, FakeMember(42),
                                   quiz_type=quiz_type or QuizType.BLIND_TEST,
                                   ping_role_id=ping_role_id)
        open_round(game, FakeMember(42),
                             self.question(quiz_type, 'Song', 'Band'))
        self.question(quiz_type, 'Spare', 'Spare Band')

    def prepare_guild(self) -> Guild:
        """Create the guild row and its host, as the first command would."""
        guild = Guild.objects.create(discord_id=1, name='Server')
        add_host(guild, user_mention(42),
                          FakeMember(42, manage_guild=True))
        return guild

    @staticmethod
    def _run(cog_function, interaction, *args, **kwargs) -> LibraryCog:
        """Run a decorated library command the way Discord would."""
        cog = LibraryCog(create_bot())
        asyncio.run(cog_function.callback(cog, interaction, *args, **kwargs))
        return cog


class TeamCommandTests(FlowTestCase):
    """The team commands fill the teams of the game played in a server."""

    def setUp(self) -> None:
        super().setUp()
        self.guild = self.prepare_guild()
        self.game = create_game(self.guild, 100, FakeMember(42))

    @staticmethod
    def _run(command, interaction, *args, **kwargs) -> FakeInteraction:
        """Run a team command the way Discord would."""
        asyncio.run(command.callback(TeamsCog(create_bot()), interaction,
                                    *args, **kwargs))
        return interaction

    @staticmethod
    def _host_interaction() -> FakeInteraction:
        return FakeInteraction()

    @staticmethod
    def _stranger_interaction() -> FakeInteraction:
        """Return an interaction of a member who is not a host."""
        interaction = FakeInteraction()
        interaction.user = FakeMember(43)
        return interaction

    def test_a_host_creates_a_team(self) -> None:
        interaction = self._run(TeamsCog.team_add,
                                self._host_interaction(), 'Reds')
        self.assertEqual([team.name for team in self.game.teams.all()],
                         ['Reds'])
        self.assertEqual(interaction.followup.sent, ['Reds created.'])

    def test_a_host_creates_a_team_with_its_first_member(self) -> None:
        interaction = self._run(TeamsCog.team_add,
                                self._host_interaction(), 'Reds',
                                FakeMember(43))
        team = self.game.teams.get()
        self.assertEqual([player.discord_user_id
                          for player in team.players.all()], [43])
        self.assertEqual(interaction.followup.sent, ['Reds created.'])

    def test_a_stranger_does_not_create_a_team(self) -> None:
        interaction = self._run(TeamsCog.team_add,
                                self._stranger_interaction(), 'Reds')
        self.assertEqual(self.game.teams.count(), 0)
        self.assertIn('Only hosts', interaction.followup.sent[0])

    def test_a_duplicated_team_name_is_refused(self) -> None:
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds')
        interaction = self._run(TeamsCog.team_add,
                                self._host_interaction(), 'reds')
        self.assertEqual(self.game.teams.count(), 1)
        self.assertIn('already has a team', interaction.followup.sent[0])

    def test_a_host_adds_a_member_to_a_team(self) -> None:
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds')
        reds = self.game.teams.get().pk
        interaction = self._run(TeamsCog.member_add,
                                self._host_interaction(), str(reds),
                                FakeMember(43))
        team = self.game.teams.get()
        self.assertEqual([player.discord_user_id
                          for player in team.players.all()], [43])
        self.assertEqual(interaction.followup.sent,
                         ['user43 joins Reds.'])

    def test_a_host_takes_a_member_out_of_a_team(self) -> None:
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds',
                   FakeMember(43))
        reds = self.game.teams.get().pk
        interaction = self._run(TeamsCog.member_remove,
                                self._host_interaction(), str(reds),
                                FakeMember(43))
        self.assertEqual(self.game.teams.get().players.count(), 0)
        self.assertEqual(interaction.followup.sent,
                         ['user43 leaves Reds.'])

    def test_a_host_renames_a_team(self) -> None:
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds')
        reds = self.game.teams.get().pk
        interaction = self._run(TeamsCog.team_rename,
                                self._host_interaction(), str(reds),
                                'Crimson')
        self.assertEqual([team.name for team in self.game.teams.all()],
                         ['Crimson'])
        self.assertEqual(interaction.followup.sent,
                         ['The team is now Crimson.'])

    def test_a_host_removes_a_team(self) -> None:
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds')
        reds = self.game.teams.get().pk
        interaction = self._run(TeamsCog.team_remove,
                                self._host_interaction(), str(reds))
        self.assertEqual(self.game.teams.count(), 0)
        self.assertEqual(interaction.followup.sent, ['Reds removed.'])

    def test_an_unknown_team_id_is_refused(self) -> None:
        interaction = self._run(TeamsCog.team_remove,
                                self._host_interaction(), '9999')
        self.assertIn('no team with that id', interaction.followup.sent[0])

    def test_a_team_of_another_game_is_refused(self) -> None:
        _, foreign = self._played_game_with_a_team()
        self.game = create_game(self.guild, 100, FakeMember(42))
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds')
        interaction = self._run(TeamsCog.team_remove,
                                self._host_interaction(), str(foreign.pk))
        self.assertIn('no team with that id', interaction.followup.sent[0])

    def test_the_picked_team_is_the_one_the_choice_offers(self) -> None:
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds')
        offered = asyncio.run(team_autocomplete(self._host_interaction(), ''))
        picked = offered[0].value
        interaction = self._run(TeamsCog.team_rename,
                                self._host_interaction(), picked, 'Crimson')
        self.assertEqual([team.name for team in self.game.teams.all()],
                         ['Crimson'])
        self.assertEqual(interaction.followup.sent,
                         ['The team is now Crimson.'])

    def test_a_host_lists_the_teams_with_their_members(self) -> None:
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds',
                   FakeMember(43))
        self._run(TeamsCog.team_add, self._host_interaction(), 'Blues')
        interaction = self._run(TeamsCog.team_list,
                                self._host_interaction())
        self.assertEqual(interaction.followup.sent,
                         ['**Blues**: nobody' + chr(10)
                          + '**Reds**: user43'])

    def test_a_game_without_a_team_says_so(self) -> None:
        interaction = self._run(TeamsCog.team_list,
                                self._host_interaction())
        self.assertIn('no team', interaction.followup.sent[0])

    def test_the_teams_of_an_ended_game_are_out_of_reach(self) -> None:
        # The command names the game being played, and an ended one is not it.
        self._run(TeamsCog.team_add, self._host_interaction(), 'Reds')
        end_game(self.game, FakeMember(42))
        interaction = self._run(TeamsCog.team_add,
                                self._host_interaction(), 'Blues')
        self.assertEqual(self.game.teams.count(), 1)
        self.assertEqual(interaction.followup.sent, [NO_GAME])

    def test_a_command_without_a_game_says_so(self) -> None:
        end_game(self.game, FakeMember(42))
        interaction = self._run(TeamsCog.team_add,
                                self._host_interaction(), 'Blues')
        self.assertEqual(interaction.followup.sent, [NO_GAME])

    def test_a_host_copies_a_team_of_another_game(self) -> None:
        past, source = self._played_game_with_a_team()
        self.game = create_game(self.guild, 100, FakeMember(42))
        interaction = self._run(TeamsCog.team_copy,
                                self._host_interaction(), str(source.pk))
        copied = self.game.teams.get()
        self.assertEqual(copied.name, source.name)
        self.assertEqual(interaction.followup.sent,
                         [f'{source.name} copied, with its members.'])
        self.assertEqual(past.teams.count(), 1)

    def test_a_copied_team_brings_its_members_along(self) -> None:
        _, source = self._played_game_with_a_team()
        source.players.add(Player.objects.from_discord(FakeMember(43)))
        self.game = create_game(self.guild, 100, FakeMember(42))
        self._run(TeamsCog.team_copy, self._host_interaction(),
                  str(source.pk))
        self.assertEqual([player.discord_user_id for player in
                          self.game.teams.get().players.all()], [43])

    def test_copying_a_team_the_game_already_has_is_refused(self) -> None:
        _, source = self._played_game_with_a_team()
        self.game = create_game(self.guild, 100, FakeMember(42))
        self._run(TeamsCog.team_add, self._host_interaction(), source.name)
        interaction = self._run(TeamsCog.team_copy,
                                self._host_interaction(), str(source.pk))
        self.assertIn('already has a team called', interaction.followup.sent[0])
        self.assertEqual(self.game.teams.count(), 1)

    def test_copying_a_team_of_another_server_is_refused(self) -> None:
        other = Guild.objects.create(discord_id=3, name='Other')
        add_host(other, user_mention(42), FakeMember(42, manage_guild=True))
        elsewhere = add_team(create_game(other, 100, FakeMember(42)),
                             FakeMember(42), 'Reds')
        interaction = self._run(TeamsCog.team_copy,
                                self._host_interaction(), str(elsewhere.pk))
        self.assertIn('another server', interaction.followup.sent[0])
        self.assertEqual(self.game.teams.count(), 0)

    def test_a_stranger_copies_no_team(self) -> None:
        _, source = self._played_game_with_a_team()
        self.game = create_game(self.guild, 100, FakeMember(42))
        interaction = self._run(TeamsCog.team_copy,
                                self._stranger_interaction(), str(source.pk))
        self.assertIn('hosts', interaction.followup.sent[0])
        self.assertEqual(self.game.teams.count(), 0)

    def test_the_teams_to_copy_are_the_ones_of_the_other_games(self) -> None:
        _, source = self._played_game_with_a_team()
        self.game = create_game(self.guild, 100, FakeMember(42))
        add_team(self.game, FakeMember(42), 'Blues')
        choices = asyncio.run(copyable_team_autocomplete(
            self._host_interaction(), ''))
        self.assertEqual([choice.value for choice in choices], [str(source.pk)])

    def test_the_teams_to_copy_are_named_after_their_game(self) -> None:
        past, _ = self._played_game_with_a_team()
        past.name = 'Friday quiz'
        past.save(update_fields=['name'])
        self.game = create_game(self.guild, 100, FakeMember(42))
        [choice] = asyncio.run(copyable_team_autocomplete(
            self._host_interaction(), ''))
        self.assertEqual(choice.name, 'Friday quiz — Blues')

    def test_the_teams_to_copy_are_searched_by_game_and_by_name(self) -> None:
        past, _ = self._played_game_with_a_team()
        past.name = 'Friday quiz'
        past.save(update_fields=['name'])
        self.game = create_game(self.guild, 100, FakeMember(42))
        for term in ('Friday', 'blues'):
            with self.subTest(term=term):
                self.assertEqual(len(asyncio.run(copyable_team_autocomplete(
                    self._host_interaction(), term))), 1)

    def test_no_team_of_a_first_game_is_offered_to_copy(self) -> None:
        add_team(self.game, FakeMember(42), 'Blues')
        self.assertEqual(asyncio.run(copyable_team_autocomplete(
            self._host_interaction(), '')), [])

    def _played_game_with_a_team(self):
        """End the game of this test case, with a team a host may copy."""
        team = add_team(self.game, FakeMember(42), 'Blues')
        end_game(self.game, FakeMember(42))
        return self.game, team


class EndGameFlowTests(FlowTestCase):
    """The panel of a closed game must be updated through its interaction."""

    @staticmethod
    def _button(view: discord.ui.View, custom_id: str):
        """Return the control of a view carrying that custom_id."""
        return next(item for item in view.children
                    if item.custom_id == custom_id)

    def _click_end(self, interaction: FakeInteraction) -> None:
        """Click the End button of a fresh panel, the way a host does."""
        async def click() -> None:
            panel = HostPanel(GameCog(create_bot()))
            await self._button(panel, HOST_END_ID).callback(interaction)
        asyncio.run(click())

    def _run_end_command(self, interaction: FakeInteraction) -> bool:
        async def run() -> bool:
            return await GameCog(create_bot()).end_game(interaction)
        return asyncio.run(run())

    def test_the_end_button_closes_its_panel(self) -> None:
        self.create_game()
        interaction = FakeInteraction(InteractionType.component)
        self._click_end(interaction)
        self.assertEqual(interaction.response.deferred, [{}])
        self.assertEqual(interaction.response.edits, [])
        [view] = interaction.original_edits
        self.assertEqual([item.custom_id for item in view.children],
                         ['blindtest_host_next', 'blindtest_host_reveal',
                          'blindtest_host_queue', 'blindtest_host_end'])
        self.assertTrue(all(item.disabled for item in view.children))
        game = Game.objects.get()
        links = ' '.join(message.jump_url
                         for message in interaction.channel.messages)
        self.assertEqual(interaction.followup.sent,
                         [f'{game.display_name} ended.',
                          f'Final scores posted: {links}.'])
        self.assertEqual(game.state, Game.State.FINISHED)
        # Ending a game publishes the round left open before the final scores.
        reveal, recap = interaction.channel.embeds
        self.assertEqual(reveal.title,
                         f'{game.display_name} — Round 1 answer')
        self.assertEqual(recap.title, f'{game.display_name} — final scores')

    def test_ending_a_game_whose_round_is_revealed_posts_only_the_recap(self) -> None:
        self.create_game()
        interaction = FakeInteraction()
        reveal_round(current_round(Game.objects.get()),
                              FakeMember(42))
        asyncio.run(GameCog(create_bot()).end_game(interaction))
        [recap] = interaction.channel.embeds
        self.assertTrue(recap.title.endswith('final scores'))

    def test_the_end_command_defers_instead_of_updating_a_message(self) -> None:
        self.create_game()
        interaction = FakeInteraction()
        self.assertTrue(self._run_end_command(interaction))
        self.assertEqual(interaction.response.deferred,
                         [{'ephemeral': True, 'thinking': True}])
        self.assertEqual(interaction.original_edits, [])
        links = ' '.join(message.jump_url
                         for message in interaction.channel.messages)
        self.assertEqual(
            interaction.followup.sent,
            [f'{Game.objects.get().display_name} ended.',
             f'Final scores posted: {links}.'])

    def test_ending_without_a_game_closes_the_stale_panel(self) -> None:
        interaction = FakeInteraction(InteractionType.component)
        self._click_end(interaction)
        [view] = interaction.original_edits
        self.assertTrue(all(item.disabled for item in view.children))
        self.assertEqual(interaction.followup.sent,
                         ['No game is running in this server.'])


class BroadcastFlowTests(FlowTestCase):
    """A post recorded without a client is made by the one holding it."""

    @staticmethod
    def _cog_posting_to(channel: FakeChannel) -> GameCog:
        """Return a cog whose client resolves the channel of a game."""
        cog = GameCog(create_bot())
        cog.broadcast_channel = lambda game: channel
        return cog

    def _record_a_round(self) -> None:
        """Open a round the way a caller without a client would."""
        game = active_game(Guild.objects.get(discord_id=1))
        reveal_round(current_round(game), FakeMember(42))
        post_round_open(game, FakeMember(42))

    def test_the_client_posts_a_recorded_round(self) -> None:
        self.create_game()
        self._record_a_round()
        channel = FakeChannel()
        cog = self._cog_posting_to(channel)
        asyncio.run(cog.flush_broadcasts())
        [message] = channel.messages
        [embed] = channel.embeds
        self.assertIn('Round 2', embed.title)
        self.assertEqual(cog.forms[message.id]['round_id'],
                         current_round(Game.objects.get()).pk)

    def test_a_recorded_post_is_made_once(self) -> None:
        self.create_game()
        self._record_a_round()
        channel = FakeChannel()
        cog = self._cog_posting_to(channel)
        asyncio.run(cog.flush_broadcasts())
        asyncio.run(cog.flush_broadcasts())
        self.assertEqual(len(channel.messages), 1)
        broadcast = Broadcast.objects.get()
        self.assertEqual(broadcast.status, Broadcast.Status.SENT)
        self.assertEqual(broadcast.message_ids, [channel.messages[0].id])

    def test_a_post_taken_by_another_client_is_left_alone(self) -> None:
        self.create_game()
        self._record_a_round()
        broadcast = Broadcast.objects.get()
        self.assertTrue(claim_broadcast(broadcast.pk))
        channel = FakeChannel()
        asyncio.run(self._cog_posting_to(channel).flush_broadcasts())
        self.assertEqual(channel.messages, [])

    def test_a_post_with_no_channel_is_marked_failed(self) -> None:
        self.create_game()
        self._record_a_round()
        cog = self._cog_posting_to(None)
        asyncio.run(cog.flush_broadcasts())
        broadcast = Broadcast.objects.get()
        self.assertEqual(broadcast.status, Broadcast.Status.FAILED)
        self.assertIn('channel is gone', broadcast.error)

    def test_the_answer_of_a_round_is_posted_too(self) -> None:
        self.create_game()
        channel = FakeChannel()
        cog = self._cog_posting_to(channel)
        game = active_game(Guild.objects.get(discord_id=1))
        post_round_reveal(current_round(game), FakeMember(42))
        asyncio.run(cog.flush_broadcasts())
        titles = [embed.title for embed in channel.embeds]
        self.assertTrue(any('answer' in title for title in titles), titles)


class AnswerFlowTests(FlowTestCase):
    """The answer form follows the round type, from its primed payload."""

    def _open_round_then_answer(self) -> tuple[FakeInteraction, FakeInteraction]:
        """Open a round as the host, then click its Answer button as a player."""
        reveal_round(Round.objects.get(), FakeMember(42))
        host = FakeInteraction()
        player = FakeInteraction(InteractionType.component)

        async def run() -> None:
            cog = GameCog(create_bot())
            await cog.open_next_round(host)
            player.message = host.channel.messages[0]
            await cog.open_guess_form(player)

        asyncio.run(run())
        return host, player

    def test_the_answer_button_uses_the_form_of_the_round(self) -> None:
        self.create_game()
        _, player = self._open_round_then_answer()
        self.assertEqual(player.response.deferred, [])
        [modal] = player.response.modals
        current = current_round(Game.objects.get())
        self.assertEqual(modal.form['round_id'], current.pk)
        self.assertEqual(modal.form['type'], QuizType.BLIND_TEST)
        self.assertIsNone(modal.pick)
        # The form is titled after the bot, as this server knows it.
        self.assertEqual(modal.title, 'user1')

    def test_the_answer_form_is_titled_after_the_bots_nickname(self) -> None:
        interaction = FakeInteraction()
        interaction.guild.me.nickname = 'Quizmaster'
        self.assertEqual(answer_form_title(interaction), 'Quizmaster')

    def test_the_answer_form_outside_a_server_keeps_its_title(self) -> None:
        interaction = FakeInteraction()
        interaction.guild = None
        self.assertEqual(answer_form_title(interaction), ANSWER_TITLE)

    def test_a_multiple_choice_round_offers_its_choices(self) -> None:
        self.create_game(quiz_type=QuizType.MULTIPLE_CHOICE)
        _, player = self._open_round_then_answer()
        [modal] = player.response.modals
        self.assertIsNone(modal.answer_input)
        self.assertEqual([option.label for option in modal.pick.options],
                         ['No Spare', 'Spare'])
        components = modal.to_dict()['components']
        self.assertEqual([component['type'] for component in components], [18, 18])
        self.assertEqual(components[0]['component']['type'], 3)

    def test_a_round_without_a_primed_form_asks_for_one_more_click(self) -> None:
        self.create_game()
        player = FakeInteraction(InteractionType.component)
        click = FakeInteraction(InteractionType.component)

        async def run() -> None:
            cog = GameCog(create_bot())
            await cog.open_guess_form(player)
            [panel] = player.followup.views
            await panel.children[0].callback(click)

        asyncio.run(run())
        self.assertEqual(player.response.deferred,
                         [{'ephemeral': True, 'thinking': True}])
        self.assertEqual(player.followup.sent, ['Your answer form is ready.'])
        self.assertEqual(player.response.modals, [])
        [modal] = click.response.modals
        self.assertEqual(modal.form['type'], QuizType.BLIND_TEST)

    def test_an_expired_form_is_reported_instead_of_failing(self) -> None:
        interaction = FakeInteraction(InteractionType.component)

        async def run() -> None:
            await GameCog(create_bot()).show_form(interaction, 999)

        asyncio.run(run())
        self.assertEqual(interaction.response.messages,
                         ['This form expired, click Answer again.'])
        self.assertEqual(interaction.response.modals, [])

    def test_a_prompt_less_round_shows_its_answer_once(self) -> None:
        self.create_game()
        reveal_round(Round.objects.get(), FakeMember(42))
        create_round(Game.objects.get(), FakeMember(42),
                              self.question('', 'Zebra', 'Stripes'))
        host = FakeInteraction()

        async def run() -> None:
            await GameCog(create_bot()).open_next_round(host)

        asyncio.run(run())
        [link] = host.channel.messages
        self.assertEqual(host.followup.sent,
                         [f'Round 2 opened: Zebra (Stripes): {link.jump_url}.'])

    def test_the_host_lines_show_the_question_and_its_answer(self) -> None:
        self.create_game(quiz_type=QuizType.MULTIPLE_CHOICE)
        reveal_round(Round.objects.get(), FakeMember(42))
        host = FakeInteraction()

        async def run() -> None:
            await GameCog(create_bot()).open_next_round(host)

        asyncio.run(run())
        [opened] = host.followup.sent
        self.assertIn('Which song? (Spare)', opened)
        self.assertIn('answer: Spare (Spare Band)', opened)

    def test_the_opened_round_links_to_the_media(self) -> None:
        self.create_game()
        Question.objects.filter(expected_answer__text='Spare').update(
            media_url='https://youtu.be/1')
        reveal_round(Round.objects.get(), FakeMember(42))
        host = FakeInteraction()

        async def run() -> None:
            await GameCog(create_bot()).open_next_round(host)

        asyncio.run(run())
        [opened] = host.followup.sent
        self.assertIn('[🎧 Listen](https://youtu.be/1)', opened)

    def test_the_queued_round_links_to_the_media(self) -> None:
        self.create_game()
        spare = Question.objects.get(expected_answer__text='Spare')
        spare.media_url = 'https://youtu.be/1'
        spare.save(update_fields=['media_url'])
        host = FakeInteraction()

        async def run() -> None:
            await GameCog(create_bot()).queue_question(host, str(spare.pk))

        asyncio.run(run())
        [queued] = host.followup.sent
        self.assertIn('[🎧 Listen](https://youtu.be/1)', queued)

    def test_the_queued_round_stays_plain_without_media(self) -> None:
        self.create_game()
        spare = Question.objects.get(expected_answer__text='Spare')
        host = FakeInteraction()

        async def run() -> None:
            await GameCog(create_bot()).queue_question(host, str(spare.pk))

        asyncio.run(run())
        [queued] = host.followup.sent
        self.assertNotIn('Listen', queued)

    def test_the_second_field_is_named_and_hinted_after_the_model(self) -> None:
        async def build() -> GuessModal:
            return GuessModal(GameCog(create_bot()), answer_form())

        modal = asyncio.run(build())
        payload = modal.to_dict()
        fields = [row['component'] for row in payload['components']]
        self.assertEqual([field['custom_id'] for field in fields],
                         ['blindtest_answer_text', 'blindtest_answer_secondary'])
        self.assertEqual([row['label'] for row in payload['components']],
                         ['Answer', 'Secondary answer'])
        self.assertFalse(fields[1]['required'])
        self.assertIn('Artist', fields[1]['placeholder'])

    def test_the_form_records_the_secondary_answer(self) -> None:
        self.create_game()
        interaction = FakeInteraction(InteractionType.modal_submit)

        async def submit() -> None:
            modal = GuessModal(GameCog(create_bot()), answer_form())
            modal.answer_input._value = 'Song'
            modal.secondary_input._value = 'Band'
            await modal.on_submit(interaction)

        asyncio.run(submit())
        guess = Guess.objects.get()
        self.assertEqual((guess.text, guess.secondary_text), ('Song', 'Band'))
        self.assertEqual(interaction.followup.sent,
                         ['Answer recorded for round 1. '
                          'The result comes with the reveal.'])

    def test_the_form_records_the_picked_choice(self) -> None:
        self.create_game(quiz_type=QuizType.MULTIPLE_CHOICE)
        question = Round.objects.get().question
        options = [{'pk': choice.pk, 'label': choice.text}
                   for choice in question.choices.all()]
        interaction = FakeInteraction(InteractionType.modal_submit)

        async def submit() -> None:
            modal = GuessModal(GameCog(create_bot()), answer_form(
                quiz_type=QuizType.MULTIPLE_CHOICE, options=options))
            chosen = next(option for option in options
                          if option['label'] == 'Song')
            modal.pick._values = [str(chosen['pk'])]
            modal.secondary_input._value = 'Band'
            await modal.on_submit(interaction)

        asyncio.run(submit())
        guess = Guess.objects.get()
        self.assertEqual((guess.text, guess.secondary_text), ('Song', 'Band'))
        self.assertTrue(guess.text_correct)
        self.assertTrue(guess.secondary_correct)


    def test_the_setup_pickers_accept_fewer_options_than_the_limit(self) -> None:
        cog = asyncio.run(_loaded_bot()).get_cog('GameCog')
        for choices, queued in (
                ([{'pk': 1, 'label': 'Song (Band)', 'media': False}], None),
                (None, [{'pk': 1, 'label': 'Song (Band)', 'media': False}]),
                (None, None)):
            with self.subTest(choices=choices, queued=queued):
                panel = SetupPanel(cog, choices, queued)
                [add, remove] = panel.children[:2]
                self.assertLessEqual(add.max_values, len(add.options))
                self.assertLessEqual(remove.max_values, len(remove.options))
                self.assertLessEqual(len(panel.to_components()), 5)
                options = [question for question in range(26)]
                panel = SetupPanel(
                    cog, [{'pk': question, 'label': f'Q{question}',
                           'media': False} for question in options], None)
                self.assertEqual(panel.children[0].max_values, 25)
                self.assertLessEqual(len(panel.to_components()), 5)


class SetupFlowTests(FlowTestCase):
    """The setup panel prepares a game that publish makes public."""

    @staticmethod
    def _control(view: discord.ui.View, custom_id: str):
        """Return the control of a view carrying that custom_id."""
        return next(item for item in view.children
                    if item.custom_id == custom_id)

    @staticmethod
    def _click(control) -> FakeInteraction:
        """Activate a setup control the way a host click would."""
        click = FakeInteraction(InteractionType.component)
        asyncio.run(control.callback(click))
        return click

    def _setup(self, name: str = 'Fiesta'):
        """Set up a game through the setup command and return its panel."""
        self.prepare_guild()
        host = FakeInteraction()
        cog = GameCog(create_bot())
        asyncio.run(cog.setup_game(host, 'STANDARD', name))
        return cog, host.followup.views[0], host

    def test_setup_opens_the_game_and_sends_its_setup_controls(self) -> None:
        _cog, panel, host = self._setup()
        self.assertIsInstance(panel, SetupPanel)
        self.assertIn('Fiesta', host.followup.sent[0])
        self.assertIn('0 question(s) queued', host.followup.sent[0])
        game = Game.objects.get()
        self.assertEqual(game.state, Game.State.SETUP)

    def test_the_invoking_channel_answers_when_the_guild_has_no_default(self) -> None:
        self.prepare_guild()
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta'))
        self.assertEqual(Game.objects.get().channel_id, 100)

    def test_the_guild_default_redirects_a_game_set_up_elsewhere(self) -> None:
        guild = self.prepare_guild()
        set_default_channel(guild, 555, FakeMember(42, manage_guild=True))
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta'))
        self.assertEqual(Game.objects.get().channel_id, 555)

    def test_a_named_channel_wins_over_the_guild_default(self) -> None:
        guild = self.prepare_guild()
        set_default_channel(guild, 555, FakeMember(42, manage_guild=True))
        channel = FakeGuildChannel(FakeGuild(1), 777, 'quiz')
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta',
                                                     channel=channel))
        self.assertEqual(Game.objects.get().channel_id, 777)

    def test_setup_plays_in_the_channel_the_host_named(self) -> None:
        self.prepare_guild()
        channel = FakeGuildChannel(FakeGuild(1), 555, 'quiz')
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta',
                                                     channel=channel))
        self.assertEqual(Game.objects.get().channel_id, 555)

    def test_setup_refuses_a_channel_of_another_server(self) -> None:
        self.prepare_guild()
        channel = FakeGuildChannel(FakeGuild(2), 555, 'quiz')
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta',
                                                     channel=channel))
        self.assertIn('another server', host.followup.sent[0])
        self.assertEqual(Game.objects.count(), 0)

    def test_setup_calls_in_the_role_the_host_named(self) -> None:
        self.prepare_guild()
        role = FakeGuild(1).role(99)
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta',
                                                     role=role))
        self.assertEqual(Game.objects.get().ping_role_id, 99)

    def test_setup_without_a_role_calls_nobody_in(self) -> None:
        self.prepare_guild()
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta'))
        self.assertIsNone(Game.objects.get().ping_role_id)

    def test_setup_refuses_a_role_of_another_server(self) -> None:
        self.prepare_guild()
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(
            host, 'STANDARD', 'Fiesta', role=FakeGuild(2).role(99)))
        self.assertIn('another server', host.followup.sent[0])
        self.assertEqual(Game.objects.count(), 0)

    def test_setup_refuses_a_role_above_the_bot(self) -> None:
        self.prepare_guild()
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(
            host, 'STANDARD', 'Fiesta', role=FakeGuild(1).role(99, position=99)))
        self.assertIn('above me', host.followup.sent[0])
        self.assertEqual(Game.objects.count(), 0)

    def test_the_add_select_queues_the_picked_questions(self) -> None:
        question = self.question('', 'Song', 'Band')
        _cog, panel, _host = self._setup()
        select = self._control(panel, SETUP_ADD_ID)
        select._values = [str(question.pk)]
        click = self._click(select)
        self.assertEqual(click.response.deferred, [{}])
        [view] = click.original_edits
        self.assertIsInstance(view, SetupPanel)
        self.assertIn('1 question(s) queued', click.original_contents[0])
        self.assertEqual(click.followup.sent,
                         ['1 question(s) added, 0 skipped.'])
        self.assertEqual(queued_count(Game.objects.get()), 1)

    def test_the_remove_select_removes_the_picked_questions(self) -> None:
        _cog, panel, _host = self._setup()
        queued = Round.objects.get(
            pk=create_round(
                                     Game.objects.get(), FakeMember(42),
                                     self.question('', 'Queued', 'Band'))['round_id'])
        select = self._control(panel, SETUP_REMOVE_ID)
        select._values = [str(queued.pk)]
        click = self._click(select)
        self.assertEqual(click.followup.sent, ['1 question(s) removed.'])
        self.assertEqual(queued_count(Game.objects.get()), 0)

    def test_the_clear_button_empties_the_queue(self) -> None:
        _cog, panel, _host = self._setup()
        create_round(Game.objects.get(), FakeMember(42),
                              self.question('', 'Queued', 'Band'))
        click = self._click(self._control(panel, SETUP_CLEAR_ID))
        self.assertEqual(click.followup.sent, ['1 question(s) removed.'])
        self.assertEqual(queued_count(Game.objects.get()), 0)

    def test_the_copy_select_queues_the_questions_of_another_game(self) -> None:
        guild = self.prepare_guild()
        older = create_game(guild, 100, FakeMember(42))
        open_round(older, FakeMember(42),
                             self.question('', 'Old', 'Band'))
        end_game(older, FakeMember(42))
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta'))
        select = self._control(host.followup.views[0], SETUP_COPY_ID)
        select._values = [str(older.pk)]
        click = self._click(select)
        self.assertEqual(click.followup.sent,
                         ['1 question(s) added, 0 skipped.'])
        self.assertEqual(
            queued_count(Game.objects.get(name='Fiesta')), 1)


    def test_publish_posts_the_game_and_swaps_in_the_host_panel(self) -> None:
        _cog, panel, _host = self._setup()
        click = self._click(self._control(panel, SETUP_PUBLISH_ID))
        game = Game.objects.get()
        self.assertEqual(game.state, Game.State.RUNNING)
        [view] = click.original_edits
        self.assertIsInstance(view, HostPanel)
        summary, controls = click.original_contents[0].split('\n')
        self.assertEqual(summary, f'**Fiesta** · Blind test · <#{game.channel_id}>')
        self.assertEqual(controls, 'Next round, reveal, queue or end.')
        [link] = click.channel.messages
        self.assertEqual(click.followup.sent,
                         [f'Fiesta is published: {link.jump_url}.'])
        [embed] = click.channel.embeds
        self.assertEqual(embed.title, 'Fiesta')

    def test_the_setup_panel_names_the_game_and_its_settings(self) -> None:
        _cog, panel, host = self._setup()
        summary = host.followup.sent[0].split('\n')[0]
        game = Game.objects.get()
        self.assertEqual(summary,
                         f'**Fiesta** · Blind test · <#{game.channel_id}>')

    def test_the_setup_panel_names_the_role_the_game_pings(self) -> None:
        guild = self.prepare_guild()
        set_default_ping_role(guild, 99, FakeMember(42, manage_guild=True))
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta'))
        summary = host.followup.sent[0].split('\n')[0]
        self.assertIn('pings <@&99>', summary)

    def test_the_host_panel_names_the_game_and_its_settings(self) -> None:
        self.prepare_guild()
        host = FakeInteraction()
        cog = GameCog(create_bot())
        asyncio.run(cog.setup_game(host, 'STANDARD', 'Fiesta'))
        asyncio.run(cog.publish(FakeInteraction()))
        opened = FakeInteraction()
        asyncio.run(cog.show_panel(opened))
        game = Game.objects.get()
        summary = opened.followup.sent[0].split('\n')[0]
        self.assertEqual(summary,
                         f'**Fiesta** · Blind test · <#{game.channel_id}>')
        self.assertIn('Next round, reveal, queue or end.',
                      opened.followup.sent[0])

    def test_a_setup_refused_over_a_prepared_game_shows_its_panel(self) -> None:
        _cog, panel, _host = self._setup()
        again = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(again, 'STANDARD', 'Other'))
        self.assertIn('being prepared', again.followup.sent[0])
        [view] = again.followup.views
        self.assertIsInstance(view, SetupPanel)

    def test_a_setup_refused_over_a_running_game_shows_the_host_panel(self) -> None:
        self.prepare_guild()
        cog = GameCog(create_bot())
        asyncio.run(cog.setup_game(FakeInteraction(), 'STANDARD', 'Fiesta'))
        asyncio.run(cog.publish(FakeInteraction()))
        again = FakeInteraction()
        asyncio.run(cog.setup_game(again, 'STANDARD', 'Other'))
        self.assertIn('already running', again.followup.sent[0])
        [view] = again.followup.views
        self.assertIsInstance(view, HostPanel)

    def test_a_refused_setup_shows_no_panel_to_someone_who_is_not_a_host(self) -> None:
        guild = self.prepare_guild()
        cog = GameCog(create_bot())
        asyncio.run(cog.setup_game(FakeInteraction(), 'STANDARD', 'Fiesta'))
        player = FakeInteraction()
        player.user = FakeMember(43)
        asyncio.run(cog.setup_game(player, 'STANDARD', 'Other',
                                   role=FakeGuild(1).role(99, position=99)))
        self.assertTrue(all(view is None for view in player.followup.views))
        self.assertIn('above me', player.followup.sent[0])

    def test_the_publication_calls_in_the_role_of_the_game(self) -> None:
        guild = self.prepare_guild()
        set_default_ping_role(guild, 99, FakeMember(42, manage_guild=True))
        host = FakeInteraction()
        cog = GameCog(create_bot())
        asyncio.run(cog.setup_game(host, 'STANDARD', 'Fiesta'))
        click = FakeInteraction(InteractionType.component)
        asyncio.run(cog.publish(click))
        self.assertEqual(click.channel.contents, ['<@&99>'])

    def test_the_publication_of_a_silent_game_calls_nobody_in(self) -> None:
        _cog, panel, _host = self._setup()
        click = self._click(self._control(panel, SETUP_PUBLISH_ID))
        self.assertEqual(click.channel.contents, [None])

    def test_a_round_calls_in_the_role_of_the_game(self) -> None:
        guild = self.prepare_guild()
        add_question(guild, FakeMember(42), 'Song')
        set_default_ping_role(guild, 99, FakeMember(42, manage_guild=True))
        cog = GameCog(create_bot())
        asyncio.run(cog.setup_game(FakeInteraction(), 'STANDARD', 'Fiesta'))
        asyncio.run(cog.publish(FakeInteraction()))
        opened = FakeInteraction()
        asyncio.run(cog.open_next_round(opened))
        self.assertEqual(opened.channel.contents, ['<@&99>'])

    def test_a_round_of_a_silent_game_calls_nobody_in(self) -> None:
        guild = self.prepare_guild()
        add_question(guild, FakeMember(42), 'Song')
        cog = GameCog(create_bot())
        asyncio.run(cog.setup_game(FakeInteraction(), 'STANDARD', 'Fiesta'))
        asyncio.run(cog.publish(FakeInteraction()))
        opened = FakeInteraction()
        asyncio.run(cog.open_next_round(opened))
        self.assertEqual(opened.channel.contents, [None])

    def test_publishing_twice_is_reported(self) -> None:
        _cog, panel, _host = self._setup()
        button = self._control(panel, SETUP_PUBLISH_ID)
        self._click(button)
        again = self._click(button)
        self.assertEqual(again.followup.sent,
                         ['This game is already published.'])
        self.assertEqual(again.original_edits, [])

    def test_ending_a_game_being_prepared_closes_it_without_a_recap(self) -> None:
        cog, _panel, _host = self._setup()
        command = FakeInteraction()
        asyncio.run(cog.end_game(command))
        self.assertEqual(Game.objects.get().state, Game.State.FINISHED)
        self.assertEqual(
            command.followup.sent,
            ['Fiesta was closed before being published.'])
        self.assertEqual(command.channel.embeds, [])

    def test_ending_a_prepared_game_from_its_panel_disables_it(self) -> None:
        _cog, panel, _host = self._setup()
        click = self._click(self._control(panel, SETUP_END_ID))
        game = Game.objects.get()
        self.assertEqual(game.state, Game.State.FINISHED)
        self.assertEqual(click.followup.sent, ['Fiesta was closed before being published.'])
        self.assertEqual(click.channel.embeds, [])
        [view] = click.original_edits
        self.assertEqual(
            [item.custom_id for item in view.children],
            ['blindtest_setup_add', 'blindtest_setup_remove',
             'blindtest_setup_copy', 'blindtest_setup_publish',
             'blindtest_setup_clear', 'blindtest_setup_end'])
        self.assertTrue(all(item.disabled for item in view.children))
        self.assertEqual(len(view.to_components()), 4)

    def test_the_panel_command_reopens_the_setup_controls(self) -> None:
        cog, _panel, _host = self._setup()
        command = FakeInteraction()
        asyncio.run(cog.show_panel(command))
        [view] = command.followup.views
        self.assertIsInstance(view, SetupPanel)
        self.assertIn('Fiesta', command.followup.sent[0])


class AdminPingTests(FlowTestCase):
    """The administrators of a server pick the role its games call in."""

    @staticmethod
    def _admin_interaction() -> FakeInteraction:
        """Return an interaction whose member may manage the server."""
        interaction = FakeInteraction()
        interaction.user = FakeMember(42, manage_guild=True)
        return interaction

    @staticmethod
    def _run(command, interaction, *args, **kwargs) -> FakeInteraction:
        """Run an admin command the way Discord would."""
        asyncio.run(command.callback(AdminCog(create_bot()), interaction,
                                    *args, **kwargs))
        return interaction

    def test_setting_the_default_ping_role_stores_the_named_one(self) -> None:
        self.prepare_guild()
        role = FakeGuild(1).role(99)
        interaction = self._run(AdminCog.ping_set, self._admin_interaction(),
                               role)
        self.assertEqual(Guild.objects.get().default_ping_role_id, 99)
        self.assertIn('<@&99>', interaction.followup.sent[0])

    def test_a_member_who_is_not_an_administrator_cannot_set_it(self) -> None:
        self.prepare_guild()
        interaction = self._run(AdminCog.ping_set, FakeInteraction(),
                                FakeGuild(1).role(99))
        self.assertIsNone(Guild.objects.get().default_ping_role_id)
        self.assertIn('Only server administrators', interaction.followup.sent[0])

    def test_a_role_above_the_bot_is_refused(self) -> None:
        self.prepare_guild()
        role = FakeGuild(1).role(99, position=99)
        interaction = self._run(AdminCog.ping_set, self._admin_interaction(),
                               role)
        self.assertIsNone(Guild.objects.get().default_ping_role_id)
        self.assertIn('above me', interaction.followup.sent[0])

    def test_clearing_the_default_ping_role_leaves_no_default(self) -> None:
        self.prepare_guild()
        self._run(AdminCog.ping_set, self._admin_interaction(),
                  FakeGuild(1).role(99))
        interaction = self._run(AdminCog.ping_clear, self._admin_interaction())
        self.assertIsNone(Guild.objects.get().default_ping_role_id)
        self.assertEqual(interaction.followup.sent,
                         ['Games now ping nobody.'])

    def test_showing_the_default_ping_role_reports_it(self) -> None:
        self.prepare_guild()
        interaction = self._run(AdminCog.ping_show, self._admin_interaction())
        self.assertIn('No default ping role', interaction.followup.sent[0])
        self._run(AdminCog.ping_set, self._admin_interaction(),
                  FakeGuild(1).role(99))
        interaction = self._run(AdminCog.ping_show, self._admin_interaction())
        self.assertIn('<@&99>', interaction.followup.sent[0])

    def test_a_game_follows_the_default_ping_role(self) -> None:
        guild = self.prepare_guild()
        self._run(AdminCog.ping_set, self._admin_interaction(),
                  FakeGuild(1).role(99))
        host = FakeInteraction()
        asyncio.run(GameCog(create_bot()).setup_game(host, 'STANDARD', 'Fiesta'))
        self.assertEqual(Game.objects.get().ping_role_id, 99)


class AdminChannelTests(FlowTestCase):
    """The administrators of a server pick the channel its games are played in."""

    @staticmethod
    def _admin_interaction() -> FakeInteraction:
        """Return an interaction whose member may manage the server."""
        interaction = FakeInteraction()
        interaction.user = FakeMember(42, manage_guild=True)
        return interaction

    @staticmethod
    def _run(command, interaction, *args, **kwargs) -> FakeInteraction:
        """Run an admin command the way Discord would."""
        asyncio.run(command.callback(AdminCog(create_bot()), interaction,
                                    *args, **kwargs))
        return interaction

    def test_setting_the_default_channel_stores_the_invoking_one(self) -> None:
        self.prepare_guild()
        interaction = self._run(AdminCog.channel_set, self._admin_interaction())
        self.assertEqual(Guild.objects.get().default_channel_id, 100)
        self.assertIn('<#100>', interaction.followup.sent[0])

    def test_setting_the_default_channel_stores_the_named_one(self) -> None:
        self.prepare_guild()
        channel = FakeGuildChannel(FakeGuild(1), 555, 'quiz')
        self._run(AdminCog.channel_set, self._admin_interaction(), channel)
        self.assertEqual(Guild.objects.get().default_channel_id, 555)

    def test_a_member_who_is_not_an_administrator_cannot_set_it(self) -> None:
        self.prepare_guild()
        interaction = FakeInteraction()
        self._run(AdminCog.channel_set, interaction)
        self.assertIsNone(Guild.objects.get().default_channel_id)
        self.assertIn('Only server administrators', interaction.followup.sent[0])

    def test_clearing_the_default_channel_leaves_no_default(self) -> None:
        self.prepare_guild()
        self._run(AdminCog.channel_set, self._admin_interaction())
        interaction = self._run(AdminCog.channel_clear, self._admin_interaction())
        self.assertIsNone(Guild.objects.get().default_channel_id)
        self.assertEqual(interaction.followup.sent,
                         ['Games are now played where they are started.'])

    def test_showing_the_default_channel_reports_it(self) -> None:
        self.prepare_guild()
        interaction = self._run(AdminCog.channel_show, self._admin_interaction())
        self.assertIn('No default channel', interaction.followup.sent[0])
        self._run(AdminCog.channel_set, self._admin_interaction())
        interaction = self._run(AdminCog.channel_show, self._admin_interaction())
        self.assertIn('<#100>', interaction.followup.sent[0])


class LibraryVariantTests(FlowTestCase):
    """The library commands split answer texts on the variant delimiter."""

    def test_question_add_registers_the_variants_of_both_answers(self) -> None:
        self.prepare_guild()
        interaction = FakeInteraction()
        self._run(LibraryCog.question_add, interaction,
                  'Song | Song (Remastered)', 'Band | The Band')
        answer = Answer.objects.get(text='Song')
        self.assertEqual([variant.text for variant in answer.variants.all()],
                         ['Song (Remastered)'])
        secondary = Answer.objects.get(text='Band')
        self.assertEqual([variant.text for variant in secondary.variants.all()],
                         ['The Band'])
        self.assertIn('1 variant(s)', interaction.followup.sent[0])

    def test_the_variant_commands_manage_the_accepted_texts(self) -> None:
        self.prepare_guild()
        Answer.objects.create(text='Song')
        added, listed, removed = (FakeInteraction(), FakeInteraction(),
                                  FakeInteraction())
        self._run(LibraryCog.variant_add, added, 'Song', 'Song (Remastered)')
        self._run(LibraryCog.variant_list, listed, 'Song')
        self._run(LibraryCog.variant_remove, removed, 'Song',
                  'song (remastered)')
        self.assertEqual(added.followup.sent,
                         ['Variant saved: **Song (Remastered)**.'])
        self.assertEqual(listed.followup.sent, ['**Song (Remastered)**'])
        self.assertEqual(removed.followup.sent,
                         ['Variant removed: **song (remastered)**.'])
        self.assertEqual(Answer.objects.get(text='Song').variants.count(), 0)

    def test_a_repeated_variant_is_refused(self) -> None:
        self.prepare_guild()
        Answer.objects.create(text='Song')
        added, repeated = FakeInteraction(), FakeInteraction()
        self._run(LibraryCog.variant_add, added, 'Song', 'Remix')
        self._run(LibraryCog.variant_add, repeated, 'Song', 'remix')
        self.assertEqual(repeated.followup.sent,
                         ['This variant is already registered.'])


class LibraryQuestionTests(FlowTestCase):
    """The question commands change the library of the server they run in."""

    def setUp(self) -> None:
        super().setUp()
        self.guild = self.prepare_guild()
        self.question = Question.objects.get(
            pk=add_question(
                                     self.guild, FakeMember(42), 'Song', prompt='Guess it',
                                     secondary_text='Band')['pk'])
        self.label = 'Guess it — answer: Song (Band)'

    def edit(self, question: str = '', user: FakeMember | None = None,
             **changes: str) -> FakeInteraction:
        """Run the edit command the way Discord would and return its reply."""
        interaction = FakeInteraction()
        interaction.user = user or FakeMember(42)
        self._run(LibraryCog.question_edit, interaction,
                  question or str(self.question.pk), **changes)
        return interaction

    def test_the_command_changes_one_field(self) -> None:
        interaction = self.edit(album='Album')
        self.assertEqual(interaction.response.deferred, [{'ephemeral': True}])
        self.question.refresh_from_db()
        self.assertEqual(self.question.album, 'Album')
        self.assertEqual(interaction.followup.sent,
                         [f'Question updated: **{self.label}**. '
                          'Changed: `album`.'])

    def test_several_options_are_changed_at_once(self) -> None:
        interaction = self.edit(album='Album', prompt='Guess', year='2001')
        self.question.refresh_from_db()
        self.assertEqual((self.question.album, self.question.year),
                         ('Album', 2001))
        self.assertEqual(
            interaction.followup.sent,
            ['Question updated: **Guess — answer: Song (Band)**. '
             'Changed: `prompt`, `year`, `album`.'])

    def test_an_option_left_empty_keeps_its_field(self) -> None:
        interaction = self.edit(prompt='', album='Album')
        self.question.refresh_from_db()
        self.assertEqual(self.question.prompt, 'Guess it')
        self.assertEqual(interaction.followup.sent,
                         [f'Question updated: **{self.label}**. '
                          'Changed: `album`.'])

    def test_a_command_without_an_option_is_reported(self) -> None:
        interaction = self.edit()
        self.assertEqual(interaction.followup.sent, ['Nothing to change.'])
        self.question.refresh_from_db()
        self.assertEqual(self.question.prompt, 'Guess it')

    def test_the_command_takes_the_same_options_as_add(self) -> None:
        added = asyncio.run(_question_options('add'))
        self.assertEqual(added, constants.EDITABLE_FIELDS)
        self.assertEqual(asyncio.run(_question_options('edit')),
                         ('question',) + added)

    def test_a_dash_drops_the_field(self) -> None:
        interaction = self.edit(prompt='-')
        self.question.refresh_from_db()
        self.assertEqual(self.question.prompt, '')
        self.assertEqual(interaction.followup.sent,
                         ['Question updated: **Song (Band)**. '
                          'Changed: `prompt`.'])

    def test_an_omitted_option_keeps_its_value(self) -> None:
        interaction = self.edit(album='Album')
        self.question.refresh_from_db()
        self.assertEqual(self.question.prompt, 'Guess it')
        self.assertEqual(interaction.followup.sent,
                         ['Question updated: **Guess it — answer: Song '
                          '(Band)**. Changed: `album`.'])

    def test_a_link_that_is_not_a_url_is_reported(self) -> None:
        interaction = self.edit(media='youtu.be/1')
        self.assertEqual(
            interaction.followup.sent,
            ['Give the media link as a full http:// or https:// URL.'])
        self.question.refresh_from_db()
        self.assertEqual(self.question.media_url, '')

    def test_the_report_shows_the_media_link_it_saved(self) -> None:
        interaction = self.edit(media='https://youtu.be/1')
        self.question.refresh_from_db()
        self.assertEqual(self.question.media_url, 'https://youtu.be/1')
        self.assertEqual(
            interaction.followup.sent,
            [f'Question updated: **{self.label}**. Changed: `media`. '
             'It links to [🎧 Listen](https://youtu.be/1).'])

    def test_renaming_the_answer_reports_the_new_one(self) -> None:
        interaction = self.edit(answer='Wonderwall | Wonderwall (Live)')
        self.question.refresh_from_db()
        self.assertEqual(self.question.expected_answer.text, 'Wonderwall')
        self.assertEqual(
            [variant.text
             for variant in self.question.expected_answer.variants.all()],
            ['Wonderwall (Live)'])
        self.assertEqual(
            interaction.followup.sent,
            ['Question updated: **Guess it — answer: Wonderwall (Band)**. '
             'Changed: `answer`.'])

    def test_a_question_that_is_gone_is_reported(self) -> None:
        interaction = self.edit(album='Album', question='999')
        self.assertEqual(
            interaction.followup.sent,
            ["This question is not in this server's library."])

    def test_a_question_id_that_is_not_one_is_reported(self) -> None:
        interaction = self.edit(album='Album', question='next')
        self.assertEqual(interaction.followup.sent,
                         ['That question no longer exists.'])

    def test_only_hosts_edit_a_question(self) -> None:
        interaction = self.edit(album='Album', user=FakeMember(43))
        self.assertTrue(interaction.followup.sent[0].startswith('Only hosts'))
        self.question.refresh_from_db()
        self.assertEqual(self.question.album, '')

    def test_the_picker_flags_a_question_that_has_a_media_link(self) -> None:
        edit_question(self.guild, FakeMember(42), self.question.pk,
                               media='https://youtu.be/1')
        choices = asyncio.run(question_autocomplete(FakeInteraction(), 'song'))
        self.assertEqual([(choice.name, choice.value) for choice in choices],
                         [(f'🎵 {self.label}', str(self.question.pk))])

    def test_question_add_saves_the_media_link(self) -> None:
        interaction = FakeInteraction()
        self._run(LibraryCog.question_add, interaction, 'Wonderwall',
                  media='https://youtu.be/1')
        question = Question.objects.get(expected_answer__text='Wonderwall')
        self.assertEqual(question.media_url, 'https://youtu.be/1')
        self.assertIn('Question saved: **Wonderwall**.',
                      interaction.followup.sent[0])

    def test_question_add_refuses_a_link_that_is_not_a_url(self) -> None:
        interaction = FakeInteraction()
        self._run(LibraryCog.question_add, interaction, 'Wonderwall',
                  media='youtu.be/1')
        self.assertEqual(
            interaction.followup.sent,
            ['Give the media link as a full http:// or https:// URL.'])
        self.assertFalse(Answer.objects.filter(text='Wonderwall').exists())


class DeferFirstTests(NoNetworkMixin, SimpleTestCase):
    """Every flow must answer its interaction before touching the database."""

    DB_CALLS = {'guild_for', 'player_for', 'run_db'}
    # Autocomplete cannot be deferred: it answers with choices within 3 seconds.
    EXEMPT = {'question_autocomplete', 'queued_autocomplete',
              'game_autocomplete', 'copyable_team_autocomplete'}
    # Helpers an already deferred flow calls, answering through its followup.
    AFTER_DEFERRING = {'answer_refused_setup', 'post_broadcast',
                       'deliver_broadcast', 'flush_broadcasts'}

    @staticmethod
    def _name(node) -> str | None:
        return getattr(node, 'id', None) or getattr(node, 'attr', None)

    def _lines(self, node, names: set[str]) -> list[int]:
        return [call.lineno for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and self._name(call.func) in names]

    def test_every_flow_defers_before_its_first_database_call(self) -> None:
        from .cogs import admin, game, library
        for module in (admin, game, library):
            for node in ast.walk(ast.parse(getsource(module))):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                if node.name in self.EXEMPT | self.AFTER_DEFERRING:
                    continue
                work = self._lines(node, self.DB_CALLS)
                if not work:
                    continue
                with self.subTest(flow=f'{module.__name__}.{node.name}'):
                    defer = self._lines(node, {'defer'})
                    self.assertTrue(defer, 'the flow never defers')
                    self.assertLess(min(defer), min(work),
                                    'the flow works on the database first')
