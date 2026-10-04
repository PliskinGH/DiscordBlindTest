"""Tests for the quiz game rules."""

from collections.abc import Iterable
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import ProtectedError
from django.db.utils import IntegrityError
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from discordcore.mentions import role_mention, user_mention
from discordcore.models import Guild, Player

from . import caching, matching, services
from .constants import BROADCAST_CLAIM_TIMEOUT, DEFAULT_BLIND_TEST_PROMPT
from .models import (Answer, AnswerVariant, Broadcast, Game, Guess, Question,
                     QuizType, Round, ScoringMode)


class FakeRole:
    """Minimal stand-in for a ``discord.Role``."""

    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakePermissions:
    """Minimal stand-in for ``discord.Permissions``."""

    def __init__(self, manage_guild: bool = False) -> None:
        self.manage_guild = manage_guild


class FakeGuild:
    """Minimal stand-in for a ``discord.Guild``."""

    def __init__(self, guild_id: int, name: str = 'Server') -> None:
        self.id = guild_id
        self.name = name


class FakeMember:
    """Minimal stand-in for a ``discord.Member``."""

    def __init__(self, user_id: int, roles: Iterable[int] = (),
                 manage_guild: bool = False) -> None:
        self.id = user_id
        self.name = f'user{user_id}'
        self.roles = [FakeRole(role_id) for role_id in roles]
        self.guild_permissions = FakePermissions(manage_guild)


class GameTestCase(TestCase):
    """A guild with its host, a plain player member and two questions."""

    def setUp(self):
        # A cached row outlives a test and would point at a rolled back one.
        cache.clear()
        self.guild = Guild.objects.create(discord_id=1, name='Server')
        self.other_guild = Guild.objects.create(discord_id=2, name='Other')
        self.host = FakeMember(42)
        self.player = FakeMember(43)
        self.admin = FakeMember(42, manage_guild=True)
        services.add_host(self.guild, user_mention(self.host.id), self.admin)
        self.host_player = Player.objects.from_discord(self.host)
        self.player_row = Player.objects.from_discord(self.player)
        self.answer = Answer.objects.create(text='Song')
        self.artist = Answer.objects.create(text='Band')
        self.question = Question.objects.create(expected_answer=self.answer,
                                                secondary_answer=self.artist)
        self.other_question = Question.objects.create(
            expected_answer=Answer.objects.create(text='Other'))

    def create_game(self, guild: Guild | None = None,
                   host_member: FakeMember | None = None) -> Game:
        """Create a game in a guild, hosted by the given member."""
        return services.create_game(guild or self.guild, 100,
                                   host_member or self.host)


class GameTests(GameTestCase):
    def test_a_listed_host_creates_a_game(self):
        game = self.create_game()
        self.assertEqual(game.state, Game.State.RUNNING)
        self.assertEqual(game.host, self.host_player)
        self.assertEqual(game.guild, self.guild)
        self.assertTrue(game.is_running)

    def test_the_host_player_row_is_created_on_demand(self):
        services.add_host(self.guild, user_mention(70), self.admin)
        game = self.create_game(host_member=FakeMember(70))
        self.assertEqual(game.host.discord_user_id, 70)
        self.assertEqual(game.host.username, 'user70')

    def test_a_member_holding_a_host_role_creates_a_game(self):
        services.add_host(self.guild, role_mention(7), self.admin)
        game = self.create_game(host_member=FakeMember(44, roles=[7]))
        self.assertEqual(game.host.discord_user_id, 44)

    def test_a_server_manager_creates_a_game(self):
        game = self.create_game(host_member=FakeMember(45, manage_guild=True))
        self.assertEqual(game.host.discord_user_id, 45)

    def test_a_plain_player_is_not_allowed_to_create_a_game(self):
        with self.assertRaises(PermissionError):
            self.create_game(host_member=self.player)

    def test_a_host_of_another_guild_is_not_allowed_here(self):
        services.add_host(self.other_guild, user_mention(99), self.admin)
        with self.assertRaises(PermissionError):
            self.create_game(host_member=FakeMember(99))

    def test_only_one_game_runs_at_a_time(self):
        self.create_game()
        with self.assertRaises(ValueError):
            self.create_game()

    def test_only_hosts_end_a_game(self):
        game = self.create_game()
        with self.assertRaises(PermissionError):
            services.finish_game(game, self.player)

    def test_finishing_reveals_the_round_in_progress(self):
        game = self.create_game()
        round_ = Round.objects.get(
            pk=services.start_round(game, self.host, self.question)['round_id'])
        services.submit_guess(round_, self.player_row, 'Song', 'Band')
        result = services.finish_game(game, self.host)
        round_.refresh_from_db()
        game.refresh_from_db()
        self.assertTrue(round_.is_revealed)
        self.assertEqual(game.state, Game.State.FINISHED)
        self.assertIsNotNone(game.finished_at)
        # The reveal travels back so the caller can publish the answer.
        self.assertEqual(result['reveal']['round_id'], round_.pk)
        self.assertEqual(result['reveal']['answer_text'], 'Song (Band)')
        self.assertEqual(result['reveal']['right'], 1)
        # The recap counts the round the ending revealed.
        self.assertEqual(result['rounds'], 1)
        self.assertEqual(result['answers'], 1)
        self.assertEqual([score['points'] for score in result['scores']], [2])

    def test_finishing_a_game_without_an_open_round_reveals_nothing(self):
        game = self.create_game()
        result = services.finish_game(game, self.host)
        self.assertIsNone(result['reveal'])
        self.assertEqual(result['rounds'], 0)

    def test_finishing_a_revealed_round_publishes_it_again(self):
        game = self.create_game()
        round_ = Round.objects.get(
            pk=services.start_round(game, self.host, self.question)['round_id'])
        services.reveal_round(round_, self.host)
        result = services.finish_game(game, self.host)
        self.assertIsNone(result['reveal'])
        self.assertEqual(result['rounds'], 1)

    def test_finishing_twice_is_refused(self):
        game = self.create_game()
        services.finish_game(game, self.host)
        with self.assertRaises(ValueError):
            services.finish_game(game, self.host)

    def test_a_new_game_can_be_created_after_the_previous_one_ended(self):
        game = self.create_game()
        services.finish_game(game, self.host)
        game = self.create_game()
        self.assertEqual(game.state, Game.State.RUNNING)

    def test_the_host_picks_the_scoring_mode(self):
        game = services.create_game(self.guild, 100, self.host,
                                   ScoringMode.FIRST_ONLY)
        self.assertEqual(game.scoring_mode, ScoringMode.FIRST_ONLY)

    def test_an_unknown_scoring_mode_is_refused(self):
        with self.assertRaises(ValueError):
            services.create_game(self.guild, 100, self.host, 'WILD')

    def test_a_guild_with_games_cannot_be_deleted(self):
        self.create_game()
        with self.assertRaises(ProtectedError):
            self.guild.delete()

    def test_a_game_without_a_channel_follows_the_guild_default(self):
        services.set_default_channel(self.guild, 555, self.admin)
        game = services.create_game(self.guild, None, self.host,
                                   invoking_id=100)
        self.assertEqual(game.channel_id, 555)

    def test_the_guild_default_wins_over_the_invoking_channel(self):
        services.set_default_channel(self.guild, 555, self.admin)
        game = services.create_game(self.guild, None, self.host,
                                   invoking_id=100)
        self.assertEqual(game.channel_id, 555)

    def test_a_game_channel_wins_over_the_guild_default(self):
        services.set_default_channel(self.guild, 555, self.admin)
        game = services.create_game(self.guild, 777, self.host, invoking_id=100)
        self.assertEqual(game.channel_id, 777)

    def test_the_invoking_channel_answers_when_nothing_else_is_known(self):
        game = services.create_game(self.guild, None, self.host, invoking_id=100)
        self.assertEqual(game.channel_id, 100)

    def test_a_game_needs_a_channel_guild_default_or_invoking_channel(self):
        with self.assertRaises(ValueError):
            services.create_game(self.guild, None, self.host)


class DefaultChannelTests(GameTestCase):
    def test_a_guild_has_no_default_channel(self):
        self.assertIsNone(services.default_channel_of(self.guild))

    def test_an_administrator_sets_the_default_channel(self):
        services.set_default_channel(self.guild, 555, self.admin)
        self.assertEqual(services.default_channel_of(self.guild), 555)
        self.assertEqual(
            Guild.objects.get(pk=self.guild.pk).default_channel_id, 555)

    def test_only_administrators_set_the_default_channel(self):
        with self.assertRaises(PermissionError):
            services.set_default_channel(self.guild, 555, self.host)
        self.assertIsNone(services.default_channel_of(self.guild))

    def test_only_administrators_clear_the_default_channel(self):
        services.set_default_channel(self.guild, 555, self.admin)
        with self.assertRaises(PermissionError):
            services.clear_default_channel(self.guild, self.host)
        self.assertEqual(services.default_channel_of(self.guild), 555)

    def test_clearing_sends_the_games_back_to_their_own_channel(self):
        services.set_default_channel(self.guild, 555, self.admin)
        services.clear_default_channel(self.guild, self.admin)
        self.assertIsNone(services.default_channel_of(self.guild))
        game = services.create_game(self.guild, 100, self.host)
        self.assertEqual(game.channel_id, 100)

    def test_the_default_is_read_from_the_database_after_a_cached_read(self):
        Guild.objects.from_discord(FakeGuild(1, 'Server'))
        services.set_default_channel(self.guild, 555, self.admin)
        self.assertEqual(
            Guild.objects.from_discord(FakeGuild(1, 'Server')).default_channel_id,
            555)

    def test_the_default_of_a_guild_is_its_own(self):
        services.set_default_channel(self.guild, 555, self.admin)
        self.assertIsNone(services.default_channel_of(self.other_guild))


class DefaultPingRoleTests(GameTestCase):
    def test_a_guild_has_no_default_ping_role(self):
        self.assertIsNone(services.default_ping_role_of(self.guild))

    def test_an_administrator_sets_the_default_ping_role(self):
        services.set_default_ping_role(self.guild, 99, self.admin)
        self.assertEqual(services.default_ping_role_of(self.guild), 99)
        self.assertEqual(
            Guild.objects.get(pk=self.guild.pk).default_ping_role_id, 99)

    def test_only_administrators_set_the_default_ping_role(self):
        with self.assertRaises(PermissionError):
            services.set_default_ping_role(self.guild, 99, self.host)
        self.assertIsNone(services.default_ping_role_of(self.guild))

    def test_only_administrators_clear_the_default_ping_role(self):
        services.set_default_ping_role(self.guild, 99, self.admin)
        with self.assertRaises(PermissionError):
            services.clear_default_ping_role(self.guild, self.host)
        self.assertEqual(services.default_ping_role_of(self.guild), 99)

    def test_clearing_leaves_the_games_calling_nobody_in(self):
        services.set_default_ping_role(self.guild, 99, self.admin)
        services.clear_default_ping_role(self.guild, self.admin)
        self.assertIsNone(services.default_ping_role_of(self.guild))
        game = services.create_game(self.guild, 100, self.host)
        self.assertIsNone(game.ping_role_id)

    def test_the_default_is_read_from_the_database_after_a_cached_read(self):
        Guild.objects.from_discord(FakeGuild(1, 'Server'))
        services.set_default_ping_role(self.guild, 99, self.admin)
        self.assertEqual(
            Guild.objects.from_discord(
                FakeGuild(1, 'Server')).default_ping_role_id, 99)

    def test_the_default_of_a_guild_is_its_own(self):
        services.set_default_ping_role(self.guild, 99, self.admin)
        self.assertIsNone(services.default_ping_role_of(self.other_guild))

    def test_a_game_without_a_role_follows_the_guild_default(self):
        services.set_default_ping_role(self.guild, 99, self.admin)
        game = services.create_game(self.guild, 100, self.host)
        self.assertEqual(game.ping_role_id, 99)

    def test_the_role_of_a_game_wins_over_the_guild_default(self):
        services.set_default_ping_role(self.guild, 99, self.admin)
        game = services.create_game(self.guild, 100, self.host,
                                    ping_role_id=77)
        self.assertEqual(game.ping_role_id, 77)

    def test_a_game_with_no_role_anywhere_calls_nobody_in(self):
        game = services.create_game(self.guild, 100, self.host)
        self.assertIsNone(game.ping_role_id)

    def test_the_role_travels_with_the_announcement_and_the_rounds(self):
        game = services.create_game(self.guild, 100, self.host,
                                    state=Game.State.SETUP, ping_role_id=99)
        announced = services.publish_game(game, self.host)
        self.assertEqual(announced['ping_role_id'], 99)
        opened = services.start_round(game, self.host, self.question)
        self.assertEqual(opened['ping_role_id'], 99)

    def test_the_announcement_of_a_silent_game_names_no_role(self):
        game = services.create_game(self.guild, 100, self.host,
                                    state=Game.State.SETUP)
        announced = services.publish_game(game, self.host)
        opened = services.start_round(game, self.host, self.question)
        self.assertIsNone(announced['ping_role_id'])
        self.assertIsNone(opened['ping_role_id'])


class RoundTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])

    def test_rounds_are_numbered_in_order(self):
        self.assertEqual(self.round.index, 1)
        services.reveal_round(self.round, self.host)
        self.assertEqual(services.start_round(self.game, self.host)['index'], 2)

    def test_only_hosts_open_or_reveal_rounds(self):
        with self.assertRaises(PermissionError):
            services.start_round(self.game, self.player)
        with self.assertRaises(PermissionError):
            services.reveal_round(self.round, self.player)

    def test_a_round_must_be_revealed_before_the_next_one(self):
        with self.assertRaises(ValueError):
            services.start_round(self.game, self.host)

    def test_a_played_question_is_not_drawn_again(self):
        services.reveal_round(self.round, self.host)
        started = Round.objects.get(
            pk=services.start_round(self.game, self.host)['round_id'])
        self.assertEqual(started.question, self.other_question)

    def test_running_out_of_questions_is_refused(self):
        services.reveal_round(self.round, self.host)
        second = Round.objects.get(
            pk=services.start_round(self.game, self.host)['round_id'])
        services.reveal_round(second, self.host)
        with self.assertRaises(ValueError):
            services.start_round(self.game, self.host)

    def test_revealing_twice_is_refused(self):
        services.reveal_round(self.round, self.host)
        with self.assertRaises(ValueError):
            services.reveal_round(self.round, self.host)

    def test_the_reveal_carries_the_answer_label_and_scores(self):
        services.submit_guess(self.round, self.player_row, 'Song', 'Band')
        result = services.reveal_round(self.round, self.host)
        self.assertEqual(result['index'], 1)
        self.assertEqual(result['question_text'], 'Song (Band)')
        self.assertEqual([row['points'] for row in result['scores']], [2])

    def test_the_reveal_refuses_a_second_reveal(self):
        services.reveal_round(self.round, self.host)
        with self.assertRaises(ValueError):
            services.reveal_round(self.round, self.host)

    def test_the_current_round_is_the_started_one(self):
        services.reveal_round(self.round, self.host)
        started = Round.objects.get(
            pk=services.start_round(self.game, self.host)['round_id'])
        self.assertEqual(services.current_round(self.game).pk, started.pk)

    def test_a_queued_round_is_not_started_on_creation(self):
        queued = Round.objects.get(
            pk=services.create_round(self.game, self.host, self.other_question)['round_id'])
        self.assertFalse(queued.is_started)
        self.assertEqual(services.current_round(self.game).pk, self.round.pk)

    def test_a_queued_round_is_started_instead_of_drawing(self):
        services.create_round(self.game, self.host, self.other_question)
        services.reveal_round(self.round, self.host)
        started = Round.objects.get(
            pk=services.start_round(self.game, self.host)['round_id'])
        self.assertEqual(started.question, self.other_question)
        self.assertTrue(started.is_started)

    def test_queued_rounds_start_in_their_order(self):
        third_question = Question.objects.create(
            expected_answer=Answer.objects.create(text='Third'))
        second = Round.objects.get(
            pk=services.create_round(self.game, self.host, self.other_question)['round_id'])
        third = Round.objects.get(
            pk=services.create_round(self.game, self.host, third_question)['round_id'])
        services.reveal_round(self.round, self.host)
        started = Round.objects.get(
            pk=services.start_round(self.game, self.host)['round_id'])
        self.assertEqual((started.pk, started.index), (second.pk, 2))
        services.reveal_round(started, self.host)
        started = Round.objects.get(
            pk=services.start_round(self.game, self.host)['round_id'])
        self.assertEqual((started.pk, started.index), (third.pk, 3))

    def test_only_hosts_queue_rounds(self):
        with self.assertRaises(PermissionError):
            services.create_round(self.game, self.player, self.other_question)

    def test_rounds_cannot_be_queued_once_the_game_is_over(self):
        services.reveal_round(self.round, self.host)
        services.finish_game(self.game, self.host)
        with self.assertRaises(ValueError):
            services.create_round(self.game, self.host, self.other_question)

class GuessTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])

    def test_a_right_guess_matches_the_title_and_the_artist(self):
        guess = services.submit_guess(self.round, self.player_row, 'Song', 'Band')
        self.assertTrue(guess.text_correct)
        self.assertTrue(guess.secondary_correct)

    def test_a_wrong_guess_scores_nothing(self):
        guess = services.submit_guess(self.round, self.player_row, 'Nope', 'Nope')
        self.assertFalse(guess.text_correct)
        self.assertFalse(guess.secondary_correct)

    def test_registered_variants_are_accepted(self):
        AnswerVariant.objects.create(answer=self.answer, text='Song (Remastered)')
        guess = services.submit_guess(self.round, self.player_row,
                                      'song (remastered)', '')
        self.assertTrue(guess.text_correct)

    def test_a_question_without_secondary_answer_needs_only_the_text(self):
        solo = Question.objects.create(
            expected_answer=Answer.objects.create(text='Paris'))
        self.round.question = solo
        self.round.save(update_fields=['question'])
        guess = services.submit_guess(self.round, self.player_row, 'Paris', '')
        self.assertTrue(guess.text_correct)
        self.assertFalse(guess.secondary_correct)

    def test_a_guess_stores_the_team_of_the_player(self):
        team = services.add_team(self.game, self.host, 'Reds', [self.player_row])
        guess = services.submit_guess(self.round, self.player_row, 'Song', '')
        self.assertEqual(guess.team, team)

    def test_a_guess_without_team_is_solo(self):
        guess = services.submit_guess(self.round, self.player_row, 'Song', '')
        self.assertIsNone(guess.team)

    def test_no_points_are_stored_on_a_guess(self):
        services.submit_guess(self.round, self.player_row, 'Song', 'Band')
        fields = [field.name for field in Guess._meta.fields]
        self.assertNotIn('points', fields)

    def test_an_empty_guess_is_refused(self):
        with self.assertRaises(ValueError):
            services.submit_guess(self.round, self.player_row, '', '   ')

    def test_the_first_guess_is_final(self):
        services.submit_guess(self.round, self.player_row, 'Song', 'Band')
        with self.assertRaises(ValueError):
            services.submit_guess(self.round, self.player_row, 'Other', 'Other')

    def test_guessing_a_revealed_round_is_refused(self):
        services.reveal_round(self.round, self.host)
        with self.assertRaises(ValueError):
            services.submit_guess(self.round, self.player_row, 'Song', 'Band')

    def test_guessing_an_unstarted_round_is_refused(self):
        queued = Round.objects.get(
            pk=services.create_round(self.game, self.host, self.other_question)['round_id'])
        with self.assertRaises(ValueError):
            services.submit_guess(queued, self.player_row, 'Other', '')

class ScoringTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        self.second = Player.objects.from_discord(FakeMember(44))

    def set_game_scoring_mode(self, mode: str) -> None:
        """Apply a scoring mode to the running game."""
        self.game.scoring_mode = mode
        self.game.save(update_fields=['scoring_mode'])
        self.round.refresh_from_db()

    def test_standard_mode_scores_every_correct_answer(self):
        services.submit_guess(self.round, self.player_row, 'Song', 'Band')
        services.submit_guess(self.round, self.second, 'Song', '')
        scores = services.game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2, 1])

    def test_first_only_mode_scores_the_first_correct_answer(self):
        self.set_game_scoring_mode(ScoringMode.FIRST_ONLY)
        services.submit_guess(self.round, self.player_row, 'Song', 'Band')
        services.submit_guess(self.round, self.second, 'Song', 'Band')
        scores = services.game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2, 0])

    def test_speed_mode_rewards_the_fastest_answers(self):
        self.set_game_scoring_mode(ScoringMode.SPEED)
        third = Player.objects.from_discord(FakeMember(45))
        services.submit_guess(self.round, self.player_row, 'Song', '')
        services.submit_guess(self.round, self.second, 'Song', '')
        services.submit_guess(self.round, third, 'Song', '')
        scores = services.game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [3, 2, 1])

    def test_the_round_scoring_mode_overrides_the_game_one(self):
        self.set_game_scoring_mode(ScoringMode.FIRST_ONLY)
        self.round.scoring_mode = ScoringMode.STANDARD
        self.round.save(update_fields=['scoring_mode'])
        services.submit_guess(self.round, self.player_row, 'Song', 'Band')
        services.submit_guess(self.round, self.second, 'Song', 'Band')
        scores = services.game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2, 2])

    def test_a_round_inherits_the_game_scoring_mode(self):
        self.set_game_scoring_mode(ScoringMode.FIRST_ONLY)
        self.assertEqual(self.round.effective_scoring_mode, ScoringMode.FIRST_ONLY)
        self.round.scoring_mode = ScoringMode.SPEED
        self.assertEqual(self.round.effective_scoring_mode, ScoringMode.SPEED)

    def test_scores_are_ranked_best_first(self):
        services.submit_guess(self.round, self.second, 'Song', 'Band')
        services.submit_guess(self.round, self.player_row, 'Song', '')
        scores = services.game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2, 1])
        self.assertEqual(scores[0]['username'], self.second.username)

class TeamTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        self.teammate = Player.objects.from_discord(FakeMember(44))

    def test_add_team_creates_it_with_its_players(self):
        team = services.add_team(self.game, self.host, ' Reds ',
                                 [self.player_row])
        self.assertEqual(team.name, 'Reds')
        self.assertEqual(list(team.players.all()), [self.player_row])
        self.assertEqual(services.team_of(self.game, self.player_row), team)

    def test_a_team_needs_a_name(self):
        with self.assertRaises(ValueError):
            services.add_team(self.game, self.host, '   ')

    def test_only_hosts_create_teams(self):
        with self.assertRaises(PermissionError):
            services.add_team(self.game, self.player, 'Reds')

    def test_a_team_name_is_unique_per_game(self):
        services.add_team(self.game, self.host, 'Reds')
        with self.assertRaises(IntegrityError):
            services.add_team(self.game, self.host, 'Reds')

    def test_assigning_a_player_moves_them_between_teams(self):
        reds = services.add_team(self.game, self.host, 'Reds', [self.player_row])
        blues = services.add_team(self.game, self.host, 'Blues')
        services.assign_player(blues, self.host, self.player_row)
        self.assertEqual(list(reds.players.all()), [])
        self.assertEqual(services.team_of(self.game, self.player_row), blues)

    def test_a_team_without_guesses_scores_zero(self):
        services.add_team(self.game, self.host, 'Reds', [self.player_row])
        self.assertEqual(services.game_team_scores(self.game),
                         [{'name': 'Reds', 'points': 0}])

    def test_team_scores_add_up_their_players_points(self):
        services.add_team(self.game, self.host, 'Reds', [self.player_row])
        services.add_team(self.game, self.host, 'Blues', [self.teammate])
        services.submit_guess(self.round, self.player_row, 'Song', 'Band')
        services.submit_guess(self.round, self.teammate, 'Song', '')
        scores = services.game_team_scores(self.game)
        self.assertEqual([(score['name'], score['points']) for score in scores],
                         [('Reds', 2), ('Blues', 1)])

    def test_answers_without_a_team_stay_out_of_the_team_scores(self):
        services.add_team(self.game, self.host, 'Reds', [self.player_row])
        services.submit_guess(self.round, self.teammate, 'Song', 'Band')
        self.assertEqual(services.game_team_scores(self.game),
                         [{'name': 'Reds', 'points': 0}])
        scores = services.game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2])


class MatchingTests(SimpleTestCase):
    def test_normalize_folds_case_accents_and_punctuation(self):
        self.assertEqual(matching.normalize('Écoute, Bébé!'), 'ecoute bebe')

    def test_an_empty_guess_never_matches(self):
        self.assertFalse(matching.answers_match('', 'Song'))
        self.assertFalse(matching.answers_match('  ', 'Song'))

    def test_answers_match_compares_normalized_texts(self):
        self.assertTrue(matching.answers_match('  song! ', 'Song'))

    def test_matches_any_accepts_the_canonical_answer(self):
        self.assertTrue(matching.matches_any('Band', 'Band'))

    def test_matches_any_accepts_a_variant(self):
        self.assertTrue(matching.matches_any('The Band', 'Band', ['The Band']))

    def test_matches_any_rejects_an_unknown_text(self):
        self.assertFalse(matching.matches_any('Other', 'Band', ['The Band']))

class HostManagementTests(GameTestCase):
    def test_an_admin_adds_a_host(self):
        host = services.add_host(self.guild, user_mention(50), self.admin)
        self.assertEqual(host.mention, '<@50>')
        self.assertIn('<@50>', services.hosts_of(self.guild))

    def test_adding_a_host_twice_changes_nothing(self):
        first = services.add_host(self.guild, user_mention(50), self.admin)
        second = services.add_host(self.guild, user_mention(50), self.admin)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.guild.hosts.filter(mention='<@50>').count(), 1)

    def test_only_admins_manage_hosts(self):
        with self.assertRaises(PermissionError):
            services.add_host(self.guild, user_mention(50), self.player)

    def test_an_admin_removes_a_host(self):
        services.add_host(self.guild, user_mention(50), self.admin)
        services.remove_host(self.guild, user_mention(50), self.admin)
        self.assertNotIn('<@50>', services.hosts_of(self.guild))

    def test_removing_an_unknown_host_is_refused(self):
        with self.assertRaises(ValueError):
            services.remove_host(self.guild, user_mention(50), self.admin)

    def test_only_admins_remove_hosts(self):
        services.add_host(self.guild, user_mention(50), self.admin)
        with self.assertRaises(PermissionError):
            services.remove_host(self.guild, user_mention(50), self.player)

    def test_adding_a_host_that_is_not_a_mention_is_refused(self):
        with self.assertRaises(ValueError) as refused:
            services.add_host(self.guild, 'not a mention', self.admin)
        self.assertIn('Enter a Discord mention', str(refused.exception))
        self.assertEqual(self.guild.hosts.count(), 1)

    def test_removing_a_host_that_is_not_a_mention_is_refused(self):
        with self.assertRaises(ValueError) as refused:
            services.remove_host(self.guild, 'not a mention', self.admin)
        self.assertIn('Enter a Discord mention', str(refused.exception))
        self.assertIn('<@42>', services.hosts_of(self.guild))


class LibraryRemovalTests(GameTestCase):
    """Dropping what no game played and no question uses."""

    def setUp(self):
        super().setUp()
        self.own_answer = self.guild.answers.create(text='Own')
        self.question = self.guild.questions.create(
            expected_answer=self.own_answer)
        services.add_host(self.other_guild, user_mention(42),
                          FakeMember(42, manage_guild=True))

    def test_a_host_drops_an_unplayed_question(self):
        services.remove_question(self.guild, self.host, self.question.pk)
        self.assertFalse(Question.objects.filter(pk=self.question.pk).exists())

    def test_a_played_question_is_kept(self):
        game = self.create_game()
        services.start_round(game, self.host, self.question)
        with self.assertRaises(ValueError) as refused:
            services.remove_question(self.guild, self.host, self.question.pk)
        self.assertIn('played in a game', str(refused.exception))
        self.assertTrue(Question.objects.filter(pk=self.question.pk).exists())

    def test_dropping_a_question_of_another_guild_is_refused(self):
        with self.assertRaises(ValueError):
            services.remove_question(self.other_guild, self.host,
                                     self.question.pk)

    def test_a_host_drops_an_unused_answer(self):
        spare = self.guild.answers.create(text='Spare')
        services.remove_answer(self.guild, self.host, spare.pk)
        self.assertFalse(self.guild.answers.filter(pk=spare.pk).exists())

    def test_an_answer_a_question_uses_is_kept(self):
        with self.assertRaises(ValueError) as refused:
            services.remove_answer(self.guild, self.host, self.own_answer.pk)
        self.assertIn('used by the question', str(refused.exception))
        self.assertTrue(
            self.guild.answers.filter(pk=self.own_answer.pk).exists())

    def test_the_refusal_names_the_question_that_uses_the_answer(self):
        self.question.prompt = 'Guess it'
        self.question.save(update_fields=['prompt'])
        with self.assertRaises(ValueError) as refused:
            services.remove_answer(self.guild, self.host, self.own_answer.pk)
        self.assertIn('Guess it', str(refused.exception))

    def test_an_unplayed_question_keeps_its_answer(self):
        with self.assertRaises(ValueError):
            services.remove_answer(self.guild, self.host, self.own_answer.pk)
        self.assertTrue(Question.objects.filter(pk=self.question.pk).exists())

    def test_dropping_an_answer_of_another_guild_is_refused(self):
        with self.assertRaises(ValueError):
            services.remove_answer(self.other_guild, self.host,
                                   self.own_answer.pk)

    def test_a_stranger_drops_nothing(self):
        with self.assertRaises(PermissionError):
            services.remove_question(self.guild, self.player, self.question.pk)
        with self.assertRaises(PermissionError):
            services.remove_answer(self.guild, self.player, self.own_answer.pk)

    def test_the_unused_lists_name_what_can_go(self):
        self.assertEqual(
            [row['pk'] for row in services.unused_questions(self.guild)],
            [self.question.pk])
        self.assertEqual(services.unused_answers(self.guild), [])

    def test_a_played_question_leaves_the_unused_list(self):
        game = self.create_game()
        services.start_round(game, self.host, self.question)
        self.assertEqual(services.unused_questions(self.guild), [])
        self.assertEqual(services.unused_answers(self.guild), [])

    def test_a_dropped_question_leaves_its_answer_behind(self):
        services.remove_question(self.guild, self.host, self.question.pk)
        self.assertEqual(services.unused_questions(self.guild), [])
        self.assertEqual(
            [answer['text'] for answer in services.unused_answers(self.guild)],
            ['Own'])
        services.remove_answer(self.guild, self.host, self.own_answer.pk)
        self.assertEqual(services.unused_answers(self.guild), [])

    def test_the_host_replaces_the_variants_of_an_answer(self):
        services.set_variants(self.guild, self.host, 'Own', ['Tune', 'Air'])
        self.assertEqual(
            [variant.text for variant in self.own_answer.variants.all()],
            ['Air', 'Tune'])
        services.set_variants(self.guild, self.host, 'Own', ['Tune'])
        self.assertEqual(
            [variant.text for variant in self.own_answer.variants.all()],
            ['Tune'])

    def test_the_host_drops_every_variant_of_an_answer(self):
        services.set_variants(self.guild, self.host, 'Own', ['Tune'])
        services.set_variants(self.guild, self.host, 'Own', [])
        self.assertEqual(self.own_answer.variants.count(), 0)

    def test_the_variants_of_an_answer_that_is_not_ours_are_refused(self):
        with self.assertRaises(ValueError):
            services.set_variants(self.guild, self.host, 'Missing', ['Tune'])


class LibraryTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.other_question.delete()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])

    def test_add_answer_creates_it_for_the_guild(self):
        answer = services.add_answer(self.guild, self.host, ' Wonderwall ')
        self.assertEqual(answer.text, 'Wonderwall')
        self.assertEqual(answer.guild, self.guild)

    def test_add_answer_is_idempotent(self):
        first = services.add_answer(self.guild, self.host, 'Wonderwall')
        second = services.add_answer(self.guild, self.host, 'wonderwall')
        self.assertEqual(first.pk, second.pk)

    def test_only_hosts_add_answers(self):
        with self.assertRaises(PermissionError):
            services.add_answer(self.guild, self.player, 'Nope')

    def test_an_answer_needs_a_text(self):
        with self.assertRaises(ValueError):
            services.add_answer(self.guild, self.host, '   ')

    def test_add_question_creates_it_for_the_guild(self):
        question = Question.objects.get(
            pk=services.add_question(
                                     self.guild, self.host, 'Song', prompt='Guess it',
                                     secondary_text='Band', year=1999, album='Album')['pk'])
        self.assertEqual(question.guild, self.guild)
        self.assertEqual(question.expected_answer.text, 'Song')
        self.assertEqual(question.expected_answer.guild, self.guild)
        self.assertEqual(question.secondary_answer.text, 'Band')
        self.assertEqual(question.prompt, 'Guess it')
        self.assertEqual(question.year, 1999)

    def test_the_media_link_of_a_question_is_kept(self):
        question = Question.objects.get(
            pk=services.add_question(self.guild, self.host, 'Wonderwall',
                                     media_url='https://youtu.be/1')['pk'])
        self.assertEqual(question.media_url, 'https://youtu.be/1')

    def test_a_media_link_must_be_a_full_url(self):
        for url in ('youtu.be/1', 'www.youtube.com/watch?v=1', ' song.mp3'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    services.add_question(self.guild, self.host, 'Wonderwall',
                                          media_url=url)
        self.assertFalse(Answer.objects.filter(text='Wonderwall').exists())

    def test_a_year_the_column_cannot_keep_is_refused(self):
        for year in (0, 10000):
            with self.subTest(year=year):
                with self.assertRaises(ValueError):
                    services.add_question(self.guild, self.host, 'Wonderwall',
                                          year=year)
        self.assertFalse(Answer.objects.filter(text='Wonderwall').exists())

    def test_only_hosts_add_questions(self):
        with self.assertRaises(PermissionError):
            services.add_question(self.guild, self.player, 'Song')

    def test_a_question_needs_an_answer(self):
        with self.assertRaises(ValueError):
            services.add_question(self.guild, self.host, '   ')

    def test_a_question_can_be_created_with_choices(self):
        question = Question.objects.get(
            pk=services.add_question(
                                     self.guild, self.host, 'Right', prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        self.assertEqual([choice.text for choice in question.choices.all()],
                         ['Right', 'Wrong'])

    def test_choices_without_a_prompt_are_refused(self):
        with self.assertRaises(ValueError):
            services.add_question(self.guild, self.host, 'Right',
                                  choices=['Right', 'Wrong'])

    def test_the_expected_answer_must_be_a_choice(self):
        with self.assertRaises(ValueError):
            services.add_question(self.guild, self.host, 'Right', prompt='Pick one',
                                  choices=['Wrong', 'Other'])

    def test_a_refused_choice_question_leaves_nothing_behind(self):
        with self.assertRaises(ValueError):
            services.add_question(self.guild, self.host, 'Right',
                                  choices=['Right', 'Wrong'])
        self.assertFalse(Answer.objects.filter(text='Right').exists())
        self.assertFalse(Question.objects.filter(prompt='').filter(
            expected_answer__text='Right').exists())

    def test_add_question_returns_a_plain_label(self):
        result = services.add_question(
            self.guild, self.host, 'Song', secondary_text='Band')
        self.assertIsInstance(result['pk'], int)
        self.assertEqual(result['label'], 'Song (Band)')

    def test_question_choices_carry_plain_labels(self):
        Question.objects.create(
            guild=self.guild,
            expected_answer=Answer.objects.create(text='Morning Bell'),
            secondary_answer=Answer.objects.create(text='Radiohead'))
        [choice] = services.question_choices(self.game)
        self.assertEqual(choice['label'], 'Morning Bell (Radiohead)')


    def test_question_choices_offer_global_and_guild_questions(self):
        global_question = Question.objects.create(
            expected_answer=Answer.objects.create(text='Global'))
        own = Question.objects.create(
            guild=self.guild, expected_answer=Answer.objects.create(text='Own'))
        Question.objects.create(
            guild=self.other_guild,
            expected_answer=Answer.objects.create(text='Foreign'))
        choices = services.question_choices(self.game)
        self.assertEqual({choice['pk'] for choice in choices},
                         {global_question.pk, own.pk})

    def test_question_choices_skip_played_questions(self):
        self.assertEqual(services.question_choices(self.game), [])

    def test_question_choices_search_prompt_and_answers(self):
        own = Question.objects.create(
            guild=self.guild, prompt='Guess the album',
            expected_answer=Answer.objects.create(text='OK Computer'))
        Question.objects.create(
            expected_answer=Answer.objects.create(text='Wonderwall'))
        found = services.question_choices(self.game, 'album')
        self.assertEqual([choice['pk'] for choice in found], [own.pk])
        found = services.question_choices(self.game, 'computer')
        self.assertEqual([choice['pk'] for choice in found], [own.pk])

    def test_pick_question_draws_a_global_question(self):
        global_question = Question.objects.create(
            expected_answer=Answer.objects.create(text='Global'))
        self.assertEqual(services.pick_question(self.game).pk,
                         global_question.pk)

    def test_pick_question_draws_the_guilds_own_question(self):
        own = Question.objects.create(
            guild=self.guild, expected_answer=Answer.objects.create(text='Own'))
        self.assertEqual(services.pick_question(self.game).pk, own.pk)

    def test_pick_question_never_draws_another_guilds_question(self):
        Question.objects.create(
            guild=self.other_guild,
            expected_answer=Answer.objects.create(text='Foreign'))
        with self.assertRaises(ValueError):
            services.pick_question(self.game)

    def test_a_round_cannot_use_another_guilds_question(self):
        foreign = Question.objects.create(
            guild=self.other_guild,
            expected_answer=Answer.objects.create(text='Foreign'))
        with self.assertRaises(ValueError):
            services.create_round(self.game, self.host, foreign)
        services.reveal_round(self.round, self.host)
        with self.assertRaises(ValueError):
            services.start_round(self.game, self.host, foreign)


class QuestionEditTests(GameTestCase):
    """A host changes the fields of a question in one go, its answers too."""

    def setUp(self):
        super().setUp()
        self.own = Question.objects.get(
            pk=services.add_question(
                self.guild, self.host, 'Song', prompt='Guess it',
                secondary_text='Band', year=1999, album='Album',
                media_url='https://youtu.be/1')['pk'])

    def edit(self, field, value=None, question=None):
        """Change one field of a question and return it reloaded."""
        question = question or self.own
        services.edit_question(self.guild, self.host, question.pk,
                               **({} if value is None else {field: value}))
        question.refresh_from_db()
        return question

    def test_a_field_is_changed_on_its_own(self):
        self.edit('prompt', 'Guess the title')
        self.assertEqual(self.own.prompt, 'Guess the title')
        self.assertEqual(self.own.expected_answer.text, 'Song')
        self.assertEqual(self.own.secondary_answer.text, 'Band')
        self.assertEqual(self.own.album, 'Album')
        self.assertEqual(self.own.year, 1999)
        self.assertEqual(self.own.media_url, 'https://youtu.be/1')

    def test_an_empty_field_is_cleared(self):
        self.edit('media', ' ')
        self.edit('year', '')
        self.edit('album', '')
        self.edit('artist', '')
        self.assertEqual(self.own.media_url, '')
        self.assertIsNone(self.own.year)
        self.assertEqual(self.own.album, '')
        self.assertIsNone(self.own.secondary_answer)
        self.assertEqual(self.own.answer_text, 'Song')

    def test_the_media_link_must_be_a_full_url(self):
        with self.assertRaises(ValueError):
            self.edit('media', 'youtu.be/1')
        self.assertEqual(self.own.media_url, 'https://youtu.be/1')

    def test_the_year_must_be_a_number_the_column_can_keep(self):
        for value in ('nineteen', '0', '10000'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.edit('year', value)
        self.assertEqual(self.own.year, 1999)

    def test_only_hosts_change_a_question(self):
        with self.assertRaises(PermissionError):
            services.edit_question(self.guild, self.player, self.own.pk,
                                   album='Album')
        with self.assertRaises(PermissionError):
            services.editable_question(self.guild, self.player, self.own.pk)

    def test_several_fields_change_in_one_call(self):
        services.edit_question(self.guild, self.host, self.own.pk,
                               prompt='Guess the title', year='2001',
                               album='Other album', media='')
        self.own.refresh_from_db()
        self.assertEqual(self.own.prompt, 'Guess the title')
        self.assertEqual(self.own.year, 2001)
        self.assertEqual(self.own.album, 'Other album')
        self.assertEqual(self.own.media_url, '')
        self.assertEqual(self.own.answer_text, 'Song (Band)')

    def test_a_field_left_out_keeps_its_value(self):
        self.edit('album', 'Other album')
        self.assertEqual(self.own.prompt, 'Guess it')
        self.assertEqual(self.own.year, 1999)
        self.assertEqual(self.own.media_url, 'https://youtu.be/1')

    def test_a_call_without_a_field_is_refused(self):
        with self.assertRaises(ValueError):
            services.edit_question(self.guild, self.host, self.own.pk)
        self.own.refresh_from_db()
        self.assertEqual(self.own.prompt, 'Guess it')

    def test_the_answers_and_the_choices_change_together(self):
        question = Question.objects.get(
            pk=services.add_question(self.guild, self.host, 'Right',
                                     prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        services.edit_question(self.guild, self.host, question.pk,
                               answer='Righter', choices='Righter, Wrong')
        question.refresh_from_db()
        self.assertEqual(question.expected_answer.text, 'Righter')
        self.assertEqual(sorted(choice.text
                                for choice in question.choices.all()),
                         ['Righter', 'Wrong'])

    def test_a_question_of_another_server_is_not_editable(self):
        foreign = Question.objects.create(
            guild=self.other_guild,
            expected_answer=Answer.objects.create(text='Foreign'))
        with self.assertRaises(ValueError):
            services.editable_question(self.guild, self.host, foreign.pk)

    def test_a_global_question_is_not_editable(self):
        with self.assertRaises(ValueError):
            services.editable_question(self.guild, self.host,
                                       self.question.pk)

    def test_a_question_that_is_gone_is_reported(self):
        for pk in (self.own.pk + 100, 'soon'):
            with self.subTest(pk=pk):
                with self.assertRaises(ValueError):
                    services.editable_question(self.guild, self.host, pk)

    def test_renaming_an_answer_creates_a_new_one_and_keeps_the_old(self):
        previous = self.own.expected_answer
        self.edit('answer', ' Wonderwall | Wonderwall (Live) ')
        self.assertEqual(self.own.expected_answer.text, 'Wonderwall')
        self.assertEqual([variant.text for variant
                          in self.own.expected_answer.variants.all()],
                         ['Wonderwall (Live)'])
        self.assertTrue(Answer.objects.filter(pk=previous.pk).exists())
        self.assertEqual(self.own.answer_text, 'Wonderwall (Band)')

    def test_renaming_an_answer_reuses_the_row_of_the_server(self):
        other = Question.objects.get(
            pk=services.add_question(self.guild, self.host, 'Wonderwall')['pk'])
        self.edit('answer', 'wonderwall')
        self.assertEqual(self.own.expected_answer, other.expected_answer)

    def test_renaming_an_answer_leaves_the_questions_using_it_alone(self):
        other = Question.objects.get(
            pk=services.add_question(self.guild, self.host, 'Wonderwall')['pk'])
        previous = self.own.expected_answer
        self.edit('answer', 'Wonderwall', question=other)
        other.refresh_from_db()
        self.assertEqual(other.expected_answer.text, 'Wonderwall')
        self.assertEqual(self.own.expected_answer, previous)

    def test_an_answer_cannot_be_cleared(self):
        with self.assertRaises(ValueError):
            self.edit('answer', '')
        self.assertEqual(self.own.expected_answer.text, 'Song')

    def test_renaming_the_answer_of_a_choice_question_swaps_its_option(self):
        question = Question.objects.get(
            pk=services.add_question(self.guild, self.host, 'Right',
                                     prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        self.edit('answer', 'Righter', question=question)
        self.assertEqual(sorted(choice.text
                                for choice in question.choices.all()),
                         ['Righter', 'Wrong'])

    def test_the_choices_are_replaced_and_dropped(self):
        question = Question.objects.get(
            pk=services.add_question(self.guild, self.host, 'Right',
                                     prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        self.edit('choices', 'Right, Other', question=question)
        self.assertEqual(sorted(choice.text
                                for choice in question.choices.all()),
                         ['Other', 'Right'])
        self.edit('choices', '', question=question)
        self.assertEqual(question.choices.count(), 0)

    def test_the_choices_must_offer_the_expected_answer(self):
        question = Question.objects.get(
            pk=services.add_question(self.guild, self.host, 'Right',
                                     prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        with self.assertRaises(ValueError):
            self.edit('choices', 'Wrong, Other', question=question)
        self.assertEqual(sorted(choice.text
                                for choice in question.choices.all()),
                         ['Right', 'Wrong'])

    def test_a_queued_round_refuses_an_edit_it_cannot_play(self):
        game = self.create_game()
        services.create_round(game, self.host, self.own, QuizType.OPEN)
        with self.assertRaises(ValueError):
            services.edit_question(self.guild, self.host, self.own.pk,
                                   prompt='')
        self.own.refresh_from_db()
        self.assertEqual(self.own.prompt, 'Guess it')

    def test_a_batch_a_queued_round_refuses_leaves_every_field_alone(self):
        game = self.create_game()
        services.create_round(game, self.host, self.own, QuizType.OPEN)
        with self.assertRaises(ValueError):
            services.edit_question(self.guild, self.host, self.own.pk,
                                   album='Other album', prompt='')
        self.own.refresh_from_db()
        self.assertEqual(self.own.prompt, 'Guess it')
        self.assertEqual(self.own.album, 'Album')

    def test_a_queued_blind_test_round_leaves_a_prompt_optional(self):
        game = self.create_game()
        services.create_round(game, self.host, self.own)
        self.edit('prompt', '')
        self.assertEqual(self.own.prompt, '')

    def test_edit_question_returns_a_plain_label(self):
        result = services.edit_question(
            self.guild, self.host, str(self.own.pk), prompt='Guess the title')
        self.assertEqual(result['pk'], self.own.pk)
        self.assertEqual(result['fields'], ['prompt'])
        self.assertEqual(result['label'],
                         'Guess the title — answer: Song (Band)')
        self.assertEqual(result['media_url'], 'https://youtu.be/1')
        self.assertEqual(result['choices'], 0)

    def test_edit_question_lists_the_fields_it_changed(self):
        result = services.edit_question(
            self.guild, self.host, str(self.own.pk), year='', album='Other')
        self.assertEqual(result['fields'], ['year', 'album'])
        self.own.refresh_from_db()
        self.assertIsNone(self.own.year)
        self.assertEqual(self.own.album, 'Other')

    def test_library_choices_list_only_this_servers_questions(self):
        Question.objects.create(
            guild=self.other_guild,
            expected_answer=Answer.objects.create(text='Foreign'))
        Question.objects.create(
            expected_answer=Answer.objects.create(text='Global'))
        self.assertEqual(
            [choice['pk'] for choice in services.library_choices(self.guild)],
            [self.own.pk])

    def test_library_choices_carry_the_label_and_the_media(self):
        [choice] = services.library_choices(self.guild)
        self.assertEqual(choice['label'], 'Guess it — answer: Song (Band)')
        self.assertTrue(choice['media'])

    def test_library_choices_search_the_prompt_and_the_answers(self):
        self.assertEqual(
            [choice['pk'] for choice in services.library_choices(self.guild,
                                                                 'band')],
            [self.own.pk])
        self.assertEqual(services.library_choices(self.guild, 'unknown'), [])


class QuizTypeTests(GameTestCase):
    """The game sets the quiz type of its rounds; a round may override it."""

    def setUp(self):
        super().setUp()
        self.game = self.create_game()

    def _prompted_question(self) -> Question:
        """Return a question an open round can play."""
        return Question.objects.create(
            prompt='Which album?',
            expected_answer=Answer.objects.create(text='Album'))

    def _choice_question(self) -> Question:
        """Return a question a multiple choice round can play."""
        expected = Answer.objects.create(text='Right')
        question = Question.objects.create(prompt='Pick one',
                                           expected_answer=expected)
        question.choices.set([expected, Answer.objects.create(text='Wrong')])
        return question

    def test_the_display_name_defaults_to_the_game_type(self):
        self.assertEqual(self.game.display_name, f'Blind test #{self.game.pk}')
        auto = services.create_game(self.other_guild, 100, self.admin,
                                   quiz_type=QuizType.MULTIPLE_CHOICE)
        self.assertEqual(auto.display_name, f'Multiple choice #{auto.pk}')

    def test_a_named_game_keeps_its_name(self):
        game = services.create_game(self.other_guild, 100, self.admin,
                                   name='  Fiesta  ')
        self.assertEqual(game.display_name, 'Fiesta')

    def test_the_state_label_is_capitalized(self):
        self.assertEqual(self.game.state_label, 'Running')
        self.assertEqual(str(self.game),
                         f'{self.game.host} - Running (#{self.game.pk})')
        self.game.state = Game.State.SETUP
        self.assertEqual(self.game.state_label, 'Being prepared')

    def test_a_round_inherits_the_game_type(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        self.assertEqual(round_.type, '')
        self.assertEqual(round_.effective_type, QuizType.BLIND_TEST)

    def test_a_round_can_override_the_game_type(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host,
                                    self._choice_question(),
                                    QuizType.MULTIPLE_CHOICE)['round_id'])
        self.assertEqual(round_.type, QuizType.MULTIPLE_CHOICE)
        self.assertEqual(round_.effective_type, QuizType.MULTIPLE_CHOICE)

    def test_an_open_round_needs_a_prompted_question(self):
        with self.assertRaises(ValueError):
            services.start_round(self.game, self.host, self.question, QuizType.OPEN)

    def test_a_multiple_choice_round_needs_two_choices(self):
        expected = Answer.objects.create(text='Alone')
        question = Question.objects.create(prompt='Pick one',
                                           expected_answer=expected)
        question.choices.set([expected])
        with self.assertRaises(ValueError):
            services.create_round(self.game, self.host, question,
                                  QuizType.MULTIPLE_CHOICE)

    def test_the_expected_answer_must_be_one_of_the_choices(self):
        question = self._prompted_question()
        question.choices.set([Answer.objects.create(text='One'),
                              Answer.objects.create(text='Two')])
        with self.assertRaises(ValueError):
            services.create_round(self.game, self.host, question,
                                  QuizType.MULTIPLE_CHOICE)

    def test_a_blind_test_round_plays_a_question_without_a_prompt(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        self.assertEqual(services.round_display(round_)['prompt'],
                         DEFAULT_BLIND_TEST_PROMPT)

    def test_an_open_round_shows_the_prompt_of_its_question(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host,
                                    self._prompted_question(), QuizType.OPEN)['round_id'])
        self.assertEqual(services.round_display(round_)['prompt'], 'Which album?')

    def test_a_draw_skips_questions_the_round_type_cannot_play(self):
        with self.assertRaises(ValueError):
            services.start_round(self.game, self.host, quiz_type=QuizType.OPEN)
        prompted = self._prompted_question()
        started = Round.objects.get(
            pk=services.start_round(self.game, self.host,
                                    quiz_type=QuizType.OPEN)['round_id'])
        self.assertEqual(started.question, prompted)

    def test_a_draw_finds_a_multiple_choice_question_with_choices(self):
        question = self._choice_question()
        started = Round.objects.get(
            pk=services.start_round(self.game, self.host,
                                    quiz_type=QuizType.MULTIPLE_CHOICE)['round_id'])
        self.assertEqual(started.question, question)

    def test_the_form_of_a_text_round_offers_no_option(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        self.assertEqual(services.guess_form(round_)['options'], [])

    def test_the_form_of_a_multiple_choice_round_offers_its_choices(self):
        round_ = Round.objects.get(
            pk=services.create_round(self.game, self.host,
                                     self._choice_question(),
                                     QuizType.MULTIPLE_CHOICE)['round_id'])
        form = services.guess_form(round_)
        self.assertEqual(form['type'], QuizType.MULTIPLE_CHOICE)
        self.assertEqual([option['label'] for option in form['options']],
                         ['Right', 'Wrong'])

    def test_the_round_counts_the_queued_questions(self):
        services.create_round(self.game, self.host, self.other_question)
        later = Question.objects.create(
            expected_answer=Answer.objects.create(text='Later'))
        services.create_round(self.game, self.host, later)
        result = services.start_round(self.game, self.host, self.question)
        self.assertEqual(result['queued'], 1)
        self.assertEqual(result['game_name'], self.game.display_name)
        self.assertEqual(result['type_label'], 'Blind test')

    def test_the_rounds_carry_the_media_link(self):
        self.other_question.media_url = 'https://youtu.be/1'
        self.other_question.save(update_fields=['media_url'])
        queued = services.create_round(self.game, self.host, self.other_question)
        started = services.start_round(self.game, self.host, self.question)
        self.assertEqual(queued['media_url'], 'https://youtu.be/1')
        self.assertEqual(started['media_url'], '')

    def test_the_reveal_counts_the_answers_and_the_right_ones(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        services.submit_guess(round_, self.player_row, 'Song', 'Band')
        result = services.reveal_round(round_, self.host)
        self.assertEqual(result['answer_text'], 'Song (Band)')
        self.assertEqual((result['answered'], result['right']), (1, 1))
        self.assertEqual(result['right_names'], ['user43'])

    def test_the_recap_summarises_the_game(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        services.submit_guess(round_, self.player_row, 'Song', 'Band')
        services.reveal_round(round_, self.host)
        recap = services.finish_game(self.game, self.host)
        self.assertEqual(recap['rounds'], 1)
        self.assertEqual(recap['answers'], 1)
        self.assertEqual(recap['game_name'], self.game.display_name)
        self.assertEqual([row['points'] for row in recap['scores']], [2])

    def test_a_queued_question_is_not_a_round_played(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        services.submit_guess(round_, self.player_row, 'Song', 'Band')
        services.reveal_round(round_, self.host)
        services.create_round(self.game, self.host, self.other_question)
        recap = services.finish_game(self.game, self.host)
        self.assertEqual(recap['rounds'], 1)
        self.assertEqual(recap['answers'], 1)
        self.assertEqual([row['points'] for row in recap['scores']], [2])

    def test_a_round_edited_in_the_admin_is_validated(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host, self.question)['round_id'])
        round_.type = QuizType.MULTIPLE_CHOICE
        with self.assertRaises(ValidationError):
            round_.full_clean()

    def test_the_question_line_shows_the_answer_only_to_a_host(self):
        prompted = self._prompted_question()
        self.assertEqual(services.question_line(self.question), 'Song (Band)')
        self.assertEqual(services.question_line(self.question, with_answer=True),
                         'Song (Band)')
        self.assertEqual(services.question_line(prompted), 'Which album?')
        self.assertEqual(
            services.question_line(prompted, with_answer=True),
            'Which album? — answer: Album')

    def test_a_question_never_shows_its_answer_when_stringified(self):
        self.assertEqual(str(self.question), f'Question #{self.question.pk}')
        self.assertEqual(str(self._prompted_question()), 'Which album?')
        self.assertNotIn('Song', str(self.question))

    def test_the_host_text_of_a_round_carries_the_answer(self):
        round_ = Round.objects.get(
            pk=services.start_round(self.game, self.host,
                                    self._prompted_question(), QuizType.OPEN)['round_id'])
        display = services.round_display(round_)
        self.assertEqual(display['question_text'], 'Which album?')
        self.assertEqual(display['host_text'], 'Which album? — answer: Album')


class SetupStateTests(GameTestCase):
    """A game is prepared in SETUP, then published to run."""

    def setup(self, **kwargs) -> dict:
        """Create a game awaiting publication and return its panel data."""
        game = services.create_game(self.guild, 100, self.host,
                                    state=Game.State.SETUP, **kwargs)
        return services.panel_data(game)

    def game_in_setup(self, **kwargs) -> Game:
        return Game.objects.get(pk=self.setup(**kwargs)['game_id'])

    def test_setup_opens_a_game_that_is_not_running_yet(self):
        data = self.setup(name='Fiesta')
        game = Game.objects.get(pk=data['game_id'])
        self.assertEqual(game.state, Game.State.SETUP)
        self.assertTrue(game.is_preparing)
        self.assertEqual(data['game_name'], 'Fiesta')
        self.assertEqual(data['queued'], 0)
        self.assertEqual({choice['pk'] for choice in data['choices']},
                         {self.question.pk, self.other_question.pk})

    def test_a_second_game_cannot_be_set_up(self):
        self.setup()
        with self.assertRaises(ValueError):
            self.setup()

    def test_a_round_cannot_be_opened_before_publishing(self):
        game = self.game_in_setup()
        with self.assertRaises(ValueError):
            services.start_round(game, self.host, self.question)

    def test_publishing_starts_the_game(self):
        game = self.game_in_setup(name='Fiesta')
        announced = services.publish_game(game, self.host)
        game.refresh_from_db()
        self.assertTrue(game.is_running)
        self.assertEqual(announced['game_name'], 'Fiesta')
        self.assertEqual(announced['queued'], 0)
        self.assertEqual(announced['type_label'], 'Blind test')

    def test_publishing_twice_is_refused(self):
        game = self.game_in_setup()
        services.publish_game(game, self.host)
        with self.assertRaises(ValueError):
            services.publish_game(game, self.host)

    def test_only_hosts_publish(self):
        game = self.game_in_setup()
        with self.assertRaises(PermissionError):
            services.publish_game(game, self.player)

    def test_closing_an_unpublished_game_finishes_it(self):
        game = self.game_in_setup()
        services.finish_game(game, self.host)
        game.refresh_from_db()
        self.assertEqual(game.state, Game.State.FINISHED)
        self.assertFalse(game.is_preparing)


class QueueTests(GameTestCase):
    """The setup panel queues, drops and copies questions."""

    def test_queueing_questions_counts_what_was_added(self):
        game = self.create_game()
        result = services.queue_questions(
            game, self.host, [self.question.pk, self.other_question.pk])
        self.assertEqual((result['added'], result['skipped']), (2, 0))
        self.assertEqual(result['queued'], 2)

    def test_only_hosts_queue_questions(self):
        game = self.create_game()
        with self.assertRaises(PermissionError):
            services.queue_questions(game, self.player, [self.question.pk])

    def test_clearing_the_queue_drops_every_queued_question(self):
        game = self.create_game()
        services.queue_questions(game, self.host, [self.question.pk])
        self.assertEqual(services.clear_queue(game, self.host), 1)
        self.assertEqual(services.queued_count(game), 0)

    def test_copying_a_game_queues_its_questions(self):
        source = self.create_game()
        services.start_round(source, self.host, self.question)
        services.finish_game(source, self.host)
        game = self.create_game()
        result = services.copy_questions_by_pk(game, self.host, source.pk)
        self.assertEqual((result['added'], result['skipped']), (1, 0))
        self.assertEqual(result['queued'], 1)

    def test_copying_the_game_itself_is_refused(self):
        game = self.create_game()
        with self.assertRaises(ValueError):
            services.copy_questions_by_pk(game, self.host, game.pk)

    def test_copying_a_game_of_another_server_is_refused(self):
        foreign = services.create_game(self.other_guild, 100, self.admin)
        services.start_round(foreign, self.admin, self.question)
        services.finish_game(foreign, self.admin)
        game = self.create_game()
        with self.assertRaises(ValueError):
            services.copy_questions_by_pk(game, self.host, foreign.pk)


class VariantTests(GameTestCase):
    """Answers accept the variant texts hosts register."""

    def test_split_answers_separates_the_variants(self):
        self.assertEqual(services.split_answers('Song | Song (Remastered)'),
                         ('Song', ['Song (Remastered)']))
        self.assertEqual(services.split_answers('  Song '), ('Song', []))
        self.assertEqual(services.split_answers(''), ('', []))

    def test_add_variant_registers_the_text(self):
        services.add_variant(self.guild, self.host, 'Song', 'Song (Remastered)')
        self.assertEqual(services.variants_of(self.guild, self.host, 'Song'),
                         ['Song (Remastered)'])

    def test_adding_the_same_variant_twice_is_refused(self):
        services.add_variant(self.guild, self.host, 'Song', 'Remix')
        with self.assertRaises(ValueError):
            services.add_variant(self.guild, self.host, 'Song', 'remix')

    def test_the_answer_itself_is_not_a_variant(self):
        with self.assertRaises(ValueError):
            services.add_variant(self.guild, self.host, 'Song', 'song')

    def test_only_hosts_manage_variants(self):
        with self.assertRaises(PermissionError):
            services.add_variant(self.guild, self.player, 'Song', 'Remix')

    def test_removing_a_variant(self):
        services.add_variant(self.guild, self.host, 'Song', 'Remix')
        services.remove_variant(self.guild, self.host, 'Song', 'remix')
        self.assertEqual(services.variants_of(self.guild, self.host, 'Song'), [])

    def test_removing_a_missing_variant_is_refused(self):
        with self.assertRaises(ValueError):
            services.remove_variant(self.guild, self.host, 'Song', 'Nope')

    def test_a_question_registers_the_variants_of_both_answers(self):
        services.add_question(self.guild, self.host, 'Encore',
                              secondary_text='Live Band',
                              expected_variants=['Encore (Live)'],
                              secondary_variants=['Live Band (Remix)'])
        self.assertEqual(services.variants_of(self.guild, self.host, 'Encore'),
                         ['Encore (Live)'])
        self.assertEqual(services.variants_of(self.guild, self.host, 'Live Band'),
                         ['Live Band (Remix)'])


class CacheTests(GameTestCase):
    """A derived list is read once, and a write drops what it changes."""

    def test_the_hosts_of_a_guild_are_read_once(self):
        services.hosts_of(self.guild)
        with self.assertNumQueries(0):
            self.assertIn('<@42>', services.hosts_of(self.guild))

    def test_a_new_host_is_seen_at_once(self):
        services.hosts_of(self.guild)
        with self.captureOnCommitCallbacks(execute=True):
            services.add_host(self.guild, user_mention(50), self.admin)
        self.assertIn('<@50>', services.hosts_of(self.guild))

    def test_a_removed_host_disappears_at_once(self):
        services.hosts_of(self.guild)
        with self.captureOnCommitCallbacks(execute=True):
            services.remove_host(self.guild, user_mention(42), self.admin)
        self.assertNotIn('<@42>', services.hosts_of(self.guild))

    def test_the_guild_row_is_read_once(self):
        member = FakeMember(7)
        guild = Guild.objects.from_discord(member)
        with self.assertNumQueries(0):
            self.assertEqual(Guild.objects.from_discord(member).pk, guild.pk)

    def test_the_player_row_is_read_once(self):
        member = FakeMember(99)
        player = Player.objects.from_discord(member)
        with self.assertNumQueries(0):
            self.assertEqual(Player.objects.from_discord(member).pk, player.pk)

    def test_the_question_options_are_read_once(self):
        game = self.create_game()
        services.question_choices(game)
        with self.assertNumQueries(0):
            self.assertTrue(services.question_choices(game))

    def test_the_question_options_search_the_labels(self):
        game = self.create_game()
        found = services.question_choices(game, 'other')
        self.assertEqual([choice['label'] for choice in found], ['Other'])

    def test_a_new_question_is_offered_at_once(self):
        game = self.create_game()
        services.question_choices(game)
        with self.captureOnCommitCallbacks(execute=True):
            services.add_question(self.guild, self.host, 'Fresh Song')
        labels = [choice['label'] for choice in services.question_choices(game)]
        self.assertTrue(any('Fresh Song' in label for label in labels))

    def test_an_edited_answer_is_seen_at_once(self):
        game = self.create_game()
        own = Question.objects.get(
            pk=services.add_question(self.guild, self.host, 'Song')['pk'])
        services.question_choices(game)
        with self.captureOnCommitCallbacks(execute=True):
            services.edit_question(self.guild, self.host, own.pk,
                                   answer='Renamed')
        labels = [choice['label'] for choice in services.question_choices(game)]
        self.assertTrue(any('Renamed' in label for label in labels))

    def test_a_played_question_leaves_the_picker(self):
        game = self.create_game()
        played = services.question_choices(game)[0]['pk']
        with self.captureOnCommitCallbacks(execute=True):
            services.start_round(game, self.host,
                                 services.question_by_pk(played))
        left = [choice['pk'] for choice in services.question_choices(game)]
        self.assertNotIn(played, left)

    def test_the_library_options_are_read_once(self):
        services.library_choices(self.guild)
        with self.assertNumQueries(0):
            self.assertEqual(services.library_choices(self.guild), [])

    def test_the_queued_options_are_read_once(self):
        game = self.create_game()
        services.create_round(game, self.host, self.question, index=1)
        services.queued_choices(game)
        with self.assertNumQueries(0):
            self.assertEqual(len(services.queued_choices(game)), 1)

    def test_the_game_options_are_read_once(self):
        self.create_game()
        services.game_choices(self.guild)
        with self.assertNumQueries(0):
            self.assertEqual(len(services.game_choices(self.guild)), 1)

    def test_the_queue_of_a_game_is_not_read_for_another_game(self):
        # A finished game clears the one-active-game constraint of the guild.
        other = services.create_game(self.guild, 100, self.host,
                                     state=Game.State.FINISHED)
        queued = self.create_game()
        services.create_round(queued, self.host, self.question, index=1)
        services.queued_choices(queued)
        self.assertEqual(services.queued_choices(other), [])
        offered = [choice['pk'] for choice in services.question_choices(other)]
        self.assertIn(self.question.pk, offered)

    def test_a_library_too_large_to_cache_falls_back_to_the_database(self):
        game = self.create_game()
        with mock.patch.object(caching, 'LIBRARY_CACHE_LIMIT', 1):
            pks = [choice['pk'] for choice in services.question_choices(game)]
        self.assertIn(self.question.pk, pks)
        self.assertIn(self.other_question.pk, pks)


class ScoreboardQueryTests(GameTestCase):
    """A scoreboard reads the rounds of a game once, whatever their number."""

    def _play(self, rounds: int) -> Game:
        """Return a game with as many played, answered and revealed rounds."""
        game = self.create_game()
        for _count in range(rounds):
            round_ = Round.objects.get(
                pk=services.start_round(game, self.host, self.question)['round_id'])
            services.submit_guess(round_, self.player_row, 'Song')
            services.reveal_round(round_, self.host)
        return game

    def test_the_player_scores_do_not_grow_with_the_rounds(self):
        def queries(rounds: int) -> int:
            game = self._play(rounds)
            with CaptureQueriesContext(connection) as captured:
                services.game_scores(game)
            services.finish_game(game, self.host)
            return len(captured)
        self.assertEqual(queries(1), queries(3))

    def test_the_recap_does_not_grow_with_the_rounds(self):
        def queries(rounds: int) -> int:
            game = self._play(rounds)
            with CaptureQueriesContext(connection) as captured:
                services.finish_game(game, self.host)
            return len(captured)
        self.assertEqual(queries(1), queries(3))


class BroadcastTests(GameTestCase):
    """The public posts a game owes, and the client that makes them."""

    def prepared_game(self) -> Game:
        """Return a game waiting to be published."""
        return services.create_game(self.guild, 100, self.host,
                                   state=Game.State.SETUP)

    def running_game(self) -> Game:
        """Return a published game with no round open."""
        return self.create_game()

    def played_game(self) -> Game:
        """Return a game with a round open in it."""
        game = self.create_game()
        services.start_round(game, self.host)
        return game

    def test_opening_a_round_records_the_post_it_owes(self):
        payload, broadcast = services.open_round(self.running_game(), self.host)
        self.assertEqual(broadcast.kind, Broadcast.Kind.ROUND)
        self.assertEqual(broadcast.status, Broadcast.Status.PENDING)
        self.assertEqual(broadcast.round_id, payload['round_id'])
        self.assertIsNone(broadcast.sent_at)

    def test_publishing_records_the_announcement_it_owes(self):
        _payload, broadcast = services.announce_game(self.prepared_game(),
                                                     self.host)
        self.assertEqual(broadcast.kind, Broadcast.Kind.ANNOUNCE)
        self.assertIsNone(broadcast.round_id)

    def test_a_refused_transition_records_no_post(self):
        with self.assertRaises(ValueError):
            services.announce_game(self.create_game(), self.host)
        self.assertEqual(Broadcast.objects.count(), 0)

    def test_a_claimed_post_is_left_to_the_client_that_owns_it(self):
        _payload, broadcast = services.open_round(
            self.running_game(), self.host, claim=True)
        self.assertEqual(broadcast.status, Broadcast.Status.CLAIMED)
        self.assertFalse(services.claim_broadcast(broadcast.pk))
        self.assertEqual(services.pending_broadcasts(), [])

    def test_only_one_client_takes_a_post(self):
        _payload, broadcast = services.open_round(self.running_game(),
                                                  self.host)
        self.assertTrue(services.claim_broadcast(broadcast.pk))
        self.assertFalse(services.claim_broadcast(broadcast.pk))

    def _expire(self, broadcast: Broadcast) -> None:
        """Make a held post look abandoned by the client that owns it."""
        Broadcast.objects.filter(pk=broadcast.pk).update(
            claimed_at=timezone.now()
            - timedelta(seconds=BROADCAST_CLAIM_TIMEOUT + 1))

    def test_a_post_held_for_too_long_is_taken_over(self):
        _payload, broadcast = services.open_round(
            self.running_game(), self.host, claim=True)
        self.assertEqual(services.pending_broadcasts(), [])
        self._expire(broadcast)
        self.assertEqual([b.pk for b in services.pending_broadcasts()],
                         [broadcast.pk])
        self.assertTrue(services.claim_broadcast(broadcast.pk))

    def test_the_posts_come_oldest_first(self):
        game = self.running_game()
        first = services.enqueue(game, Broadcast.Kind.ANNOUNCE)
        second = services.enqueue(game, Broadcast.Kind.RECAP)
        self.assertEqual([b.pk for b in services.pending_broadcasts()],
                         [first.pk, second.pk])

    def test_a_finished_game_owes_the_answer_and_the_scores(self):
        _payload, posts = services.close_game(self.played_game(), self.host)
        self.assertEqual([broadcast.kind for broadcast, _payload in posts],
                         [Broadcast.Kind.REVEAL, Broadcast.Kind.RECAP])

    def test_a_game_ended_with_no_open_round_owes_only_the_scores(self):
        _payload, posts = services.close_game(self.create_game(), self.host)
        self.assertEqual([broadcast.kind for broadcast, _payload in posts],
                         [Broadcast.Kind.RECAP])

    def test_a_post_read_later_shows_what_it_showed(self):
        payload, broadcast = services.open_round(self.running_game(), self.host)
        self.assertEqual(services.broadcast_payload(broadcast), payload)

    def test_the_answer_and_the_scores_read_the_same_later(self):
        payload, posts = services.close_game(self.played_game(), self.host)
        reveal, recap = posts
        self.assertEqual(services.broadcast_payload(reveal[0]), payload['reveal'])
        self.assertEqual(
            services.broadcast_payload(recap[0]),
            {key: value for key, value in payload.items() if key != 'reveal'})

    def test_a_post_keeps_the_messages_it_produced(self):
        _payload, broadcast = services.open_round(self.running_game(),
                                                  self.host)
        services.mark_broadcast_sent(broadcast, [11, 12])
        self.assertEqual(broadcast.status, Broadcast.Status.SENT)
        self.assertEqual(broadcast.message_ids, [11, 12])
        self.assertIsNotNone(broadcast.sent_at)
        self.assertEqual(services.pending_broadcasts(), [])

    def test_a_post_that_could_not_be_made_keeps_its_reason(self):
        _payload, broadcast = services.open_round(self.running_game(),
                                                  self.host)
        services.mark_broadcast_failed(broadcast, 'its channel is gone')
        self.assertEqual(broadcast.status, Broadcast.Status.FAILED)
        self.assertEqual(broadcast.error, 'its channel is gone')
        # A failed post is not retried behind the caller's back.
        self.assertEqual(services.pending_broadcasts(), [])
