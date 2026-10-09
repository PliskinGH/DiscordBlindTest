"""Tests for the game rules."""

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

from discordblindtest.testing import NoNetworkMixin
from discordcore.mentions import role_mention, user_mention
from discordcore.models import Guild, Player

from . import caching, matching
from .services.broadcasts import (post_publication, broadcast_payload,
                                  claim_broadcast, post_game_end,
                                  enqueue, mark_broadcast_failed,
                                  mark_broadcast_sent, post_round_open,
                                  pending_broadcasts)
from .services.games import (clear_queue, copy_questions_by_pk,
                             create_game, end_game, game_choices,
                             panel_data, publish_game,
                             queue_questions, queued_choices)
from .services.guessing import (guess_form, round_display,
                                submit_guess)
from .services.guilds import (add_host, clear_default_channel,
                              clear_default_ping_role,
                              default_channel_of,
                              default_ping_role_of, hosts_of,
                              remove_host, set_default_channel,
                              set_default_ping_role)
from .services.library import (add_answer, add_question, add_variant,
                               edit_question, editable_question,
                               library_choices, own_questions,
                               question_by_pk,
                               question_choices, question_line,
                               remove_answer, remove_question,
                               remove_variant, set_show_all_questions,
                               set_variants,
                               split_answers, unused_answers,
                               unused_questions, variants_of)
from .services.rounds import (create_round, current_round,
                              pick_question, queued_count,
                              reveal_round, open_round, round_guesses,
                              set_guess_correctness)
from .services.scores import game_scores
from .services.teams import (add_team, assign_player, assign_players,
                            copyable_teams, copy_team, copy_team_by_pk,
                            remove_player, remove_team, rename_team,
                            team_by_pk, team_of, teams_of)

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


class GameTestCase(NoNetworkMixin, TestCase):
    """A guild with its host, a plain player member and two questions."""

    def setUp(self):
        # A cached row outlives a test and would point at a rolled back one.
        cache.clear()
        self.guild = Guild.objects.create(discord_id=1, name='Server')
        self.other_guild = Guild.objects.create(discord_id=2, name='Other')
        self.host = FakeMember(42)
        self.player = FakeMember(43)
        self.admin = FakeMember(42, manage_guild=True)
        add_host(self.guild, user_mention(self.host.id), self.admin)
        self.host_player = Player.objects.from_discord(self.host)
        self.player_row = Player.objects.from_discord(self.player)
        self.answer = Answer.objects.create(text='Song')
        self.artist = Answer.objects.create(text='Band')
        self.question = Question.objects.create(expected_answer=self.answer,
                                                secondary_answer=self.artist,
                                                author=self.host_player)
        self.other_question = Question.objects.create(
            expected_answer=Answer.objects.create(text='Other'),
            author=self.host_player)

    def create_game(self, guild: Guild | None = None,
                   host_member: FakeMember | None = None) -> Game:
        """Create a game in a guild, hosted by the given member."""
        return create_game(guild or self.guild, 100,
                                   host_member or self.host)


class GameTests(GameTestCase):
    def test_a_listed_host_creates_a_game(self):
        game = self.create_game()
        self.assertEqual(game.state, Game.State.RUNNING)
        self.assertEqual(game.host, self.host_player)
        self.assertEqual(game.guild, self.guild)
        self.assertTrue(game.is_running)

    def test_the_host_player_row_is_created_on_demand(self):
        add_host(self.guild, user_mention(70), self.admin)
        game = self.create_game(host_member=FakeMember(70))
        self.assertEqual(game.host.discord_user_id, 70)
        self.assertEqual(game.host.username, 'user70')

    def test_a_member_holding_a_host_role_creates_a_game(self):
        add_host(self.guild, role_mention(7), self.admin)
        game = self.create_game(host_member=FakeMember(44, roles=[7]))
        self.assertEqual(game.host.discord_user_id, 44)

    def test_a_server_manager_creates_a_game(self):
        game = self.create_game(host_member=FakeMember(45, manage_guild=True))
        self.assertEqual(game.host.discord_user_id, 45)

    def test_a_plain_player_is_not_allowed_to_create_a_game(self):
        with self.assertRaises(PermissionError):
            self.create_game(host_member=self.player)

    def test_a_host_of_another_guild_is_not_allowed_here(self):
        add_host(self.other_guild, user_mention(99), self.admin)
        with self.assertRaises(PermissionError):
            self.create_game(host_member=FakeMember(99))

    def test_only_one_game_runs_at_a_time(self):
        self.create_game()
        with self.assertRaises(ValueError):
            self.create_game()

    def test_only_hosts_end_a_game(self):
        game = self.create_game()
        with self.assertRaises(PermissionError):
            end_game(game, self.player)

    def test_finishing_reveals_the_round_in_progress(self):
        game = self.create_game()
        round_ = Round.objects.get(
            pk=open_round(game, self.host, self.question)['round_id'])
        submit_guess(round_, self.player_row, 'Song', 'Band')
        result = end_game(game, self.host)
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
        self.assertEqual(result['guesses'], 1)
        self.assertEqual([score['points'] for score in result['scores']], [2])

    def test_finishing_a_game_without_an_open_round_reveals_nothing(self):
        game = self.create_game()
        result = end_game(game, self.host)
        self.assertIsNone(result['reveal'])
        self.assertEqual(result['rounds'], 0)

    def test_finishing_a_revealed_round_publishes_it_again(self):
        game = self.create_game()
        round_ = Round.objects.get(
            pk=open_round(game, self.host, self.question)['round_id'])
        reveal_round(round_, self.host)
        result = end_game(game, self.host)
        self.assertIsNone(result['reveal'])
        self.assertEqual(result['rounds'], 1)

    def test_finishing_twice_is_refused(self):
        game = self.create_game()
        end_game(game, self.host)
        with self.assertRaises(ValueError):
            end_game(game, self.host)

    def test_a_new_game_can_be_created_after_the_previous_one_ended(self):
        game = self.create_game()
        end_game(game, self.host)
        game = self.create_game()
        self.assertEqual(game.state, Game.State.RUNNING)

    def test_the_host_picks_the_scoring_mode(self):
        game = create_game(self.guild, 100, self.host,
                                   ScoringMode.FIRST_ONLY)
        self.assertEqual(game.scoring_mode, ScoringMode.FIRST_ONLY)

    def test_an_unknown_scoring_mode_is_refused(self):
        with self.assertRaises(ValueError):
            create_game(self.guild, 100, self.host, 'WILD')

    def test_a_guild_with_games_cannot_be_deleted(self):
        self.create_game()
        with self.assertRaises(ProtectedError):
            self.guild.delete()

    def test_a_game_without_a_channel_follows_the_guild_default(self):
        set_default_channel(self.guild, 555, self.admin)
        game = create_game(self.guild, None, self.host,
                                   invoking_id=100)
        self.assertEqual(game.channel_id, 555)

    def test_the_guild_default_wins_over_the_invoking_channel(self):
        set_default_channel(self.guild, 555, self.admin)
        game = create_game(self.guild, None, self.host,
                                   invoking_id=100)
        self.assertEqual(game.channel_id, 555)

    def test_a_game_channel_wins_over_the_guild_default(self):
        set_default_channel(self.guild, 555, self.admin)
        game = create_game(self.guild, 777, self.host, invoking_id=100)
        self.assertEqual(game.channel_id, 777)

    def test_the_invoking_channel_answers_when_nothing_else_is_known(self):
        game = create_game(self.guild, None, self.host, invoking_id=100)
        self.assertEqual(game.channel_id, 100)

    def test_a_game_needs_a_channel_guild_default_or_invoking_channel(self):
        with self.assertRaises(ValueError):
            create_game(self.guild, None, self.host)


class DefaultChannelTests(GameTestCase):
    def test_a_guild_has_no_default_channel(self):
        self.assertIsNone(default_channel_of(self.guild))

    def test_an_administrator_sets_the_default_channel(self):
        set_default_channel(self.guild, 555, self.admin)
        self.assertEqual(default_channel_of(self.guild), 555)
        self.assertEqual(
            Guild.objects.get(pk=self.guild.pk).default_channel_id, 555)

    def test_only_administrators_set_the_default_channel(self):
        with self.assertRaises(PermissionError):
            set_default_channel(self.guild, 555, self.host)
        self.assertIsNone(default_channel_of(self.guild))

    def test_only_administrators_clear_the_default_channel(self):
        set_default_channel(self.guild, 555, self.admin)
        with self.assertRaises(PermissionError):
            clear_default_channel(self.guild, self.host)
        self.assertEqual(default_channel_of(self.guild), 555)

    def test_clearing_sends_the_games_back_to_their_own_channel(self):
        set_default_channel(self.guild, 555, self.admin)
        clear_default_channel(self.guild, self.admin)
        self.assertIsNone(default_channel_of(self.guild))
        game = create_game(self.guild, 100, self.host)
        self.assertEqual(game.channel_id, 100)

    def test_the_default_is_read_from_the_database_after_a_cached_read(self):
        Guild.objects.from_discord(FakeGuild(1, 'Server'))
        set_default_channel(self.guild, 555, self.admin)
        self.assertEqual(
            Guild.objects.from_discord(FakeGuild(1, 'Server')).default_channel_id,
            555)

    def test_the_default_of_a_guild_is_its_own(self):
        set_default_channel(self.guild, 555, self.admin)
        self.assertIsNone(default_channel_of(self.other_guild))


class DefaultPingRoleTests(GameTestCase):
    def test_a_guild_has_no_default_ping_role(self):
        self.assertIsNone(default_ping_role_of(self.guild))

    def test_an_administrator_sets_the_default_ping_role(self):
        set_default_ping_role(self.guild, 99, self.admin)
        self.assertEqual(default_ping_role_of(self.guild), 99)
        self.assertEqual(
            Guild.objects.get(pk=self.guild.pk).default_ping_role_id, 99)

    def test_only_administrators_set_the_default_ping_role(self):
        with self.assertRaises(PermissionError):
            set_default_ping_role(self.guild, 99, self.host)
        self.assertIsNone(default_ping_role_of(self.guild))

    def test_only_administrators_clear_the_default_ping_role(self):
        set_default_ping_role(self.guild, 99, self.admin)
        with self.assertRaises(PermissionError):
            clear_default_ping_role(self.guild, self.host)
        self.assertEqual(default_ping_role_of(self.guild), 99)

    def test_clearing_leaves_the_games_calling_nobody_in(self):
        set_default_ping_role(self.guild, 99, self.admin)
        clear_default_ping_role(self.guild, self.admin)
        self.assertIsNone(default_ping_role_of(self.guild))
        game = create_game(self.guild, 100, self.host)
        self.assertIsNone(game.ping_role_id)

    def test_the_default_is_read_from_the_database_after_a_cached_read(self):
        Guild.objects.from_discord(FakeGuild(1, 'Server'))
        set_default_ping_role(self.guild, 99, self.admin)
        self.assertEqual(
            Guild.objects.from_discord(
                FakeGuild(1, 'Server')).default_ping_role_id, 99)

    def test_the_default_of_a_guild_is_its_own(self):
        set_default_ping_role(self.guild, 99, self.admin)
        self.assertIsNone(default_ping_role_of(self.other_guild))

    def test_a_game_without_a_role_follows_the_guild_default(self):
        set_default_ping_role(self.guild, 99, self.admin)
        game = create_game(self.guild, 100, self.host)
        self.assertEqual(game.ping_role_id, 99)

    def test_the_role_of_a_game_wins_over_the_guild_default(self):
        set_default_ping_role(self.guild, 99, self.admin)
        game = create_game(self.guild, 100, self.host,
                                    ping_role_id=77)
        self.assertEqual(game.ping_role_id, 77)

    def test_a_game_with_no_role_anywhere_calls_nobody_in(self):
        game = create_game(self.guild, 100, self.host)
        self.assertIsNone(game.ping_role_id)

    def test_the_role_travels_with_the_publication_and_the_rounds(self):
        game = create_game(self.guild, 100, self.host,
                                    state=Game.State.SETUP, ping_role_id=99)
        published = publish_game(game, self.host)
        self.assertEqual(published['ping_role_id'], 99)
        opened = open_round(game, self.host, self.question)
        self.assertEqual(opened['ping_role_id'], 99)

    def test_the_publication_of_a_silent_game_names_no_role(self):
        game = create_game(self.guild, 100, self.host,
                                    state=Game.State.SETUP)
        published = publish_game(game, self.host)
        opened = open_round(game, self.host, self.question)
        self.assertIsNone(published['ping_role_id'])
        self.assertIsNone(opened['ping_role_id'])


class RoundTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])

    def test_rounds_are_numbered_in_order(self):
        self.assertEqual(self.round.index, 1)
        reveal_round(self.round, self.host)
        self.assertEqual(open_round(self.game, self.host)['index'], 2)

    def test_only_hosts_open_or_reveal_rounds(self):
        with self.assertRaises(PermissionError):
            open_round(self.game, self.player)
        with self.assertRaises(PermissionError):
            reveal_round(self.round, self.player)

    def test_a_round_must_be_revealed_before_the_next_one(self):
        with self.assertRaises(ValueError):
            open_round(self.game, self.host)

    def test_a_played_question_is_not_drawn_again(self):
        reveal_round(self.round, self.host)
        started = Round.objects.get(
            pk=open_round(self.game, self.host)['round_id'])
        self.assertEqual(started.question, self.other_question)

    def test_running_out_of_questions_is_refused(self):
        reveal_round(self.round, self.host)
        second = Round.objects.get(
            pk=open_round(self.game, self.host)['round_id'])
        reveal_round(second, self.host)
        with self.assertRaises(ValueError):
            open_round(self.game, self.host)

    def test_revealing_twice_is_refused(self):
        reveal_round(self.round, self.host)
        with self.assertRaises(ValueError):
            reveal_round(self.round, self.host)

    def test_the_reveal_carries_the_answer_label_and_scores(self):
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        result = reveal_round(self.round, self.host)
        self.assertEqual(result['index'], 1)
        self.assertEqual(result['question_text'], 'Song (Band)')
        self.assertEqual([row['points'] for row in result['scores']], [2])

    def test_the_reveal_refuses_a_second_reveal(self):
        reveal_round(self.round, self.host)
        with self.assertRaises(ValueError):
            reveal_round(self.round, self.host)

    def test_the_current_round_is_the_started_one(self):
        reveal_round(self.round, self.host)
        started = Round.objects.get(
            pk=open_round(self.game, self.host)['round_id'])
        self.assertEqual(current_round(self.game).pk, started.pk)

    def test_a_queued_round_is_not_started_on_creation(self):
        queued = Round.objects.get(
            pk=create_round(self.game, self.host, self.other_question)['round_id'])
        self.assertFalse(queued.is_started)
        self.assertEqual(current_round(self.game).pk, self.round.pk)

    def test_a_queued_round_is_started_instead_of_drawing(self):
        create_round(self.game, self.host, self.other_question)
        reveal_round(self.round, self.host)
        started = Round.objects.get(
            pk=open_round(self.game, self.host)['round_id'])
        self.assertEqual(started.question, self.other_question)
        self.assertTrue(started.is_started)

    def test_queued_rounds_start_in_their_order(self):
        third_question = Question.objects.create(
            expected_answer=Answer.objects.create(text='Third'))
        second = Round.objects.get(
            pk=create_round(self.game, self.host, self.other_question)['round_id'])
        third = Round.objects.get(
            pk=create_round(self.game, self.host, third_question)['round_id'])
        reveal_round(self.round, self.host)
        started = Round.objects.get(
            pk=open_round(self.game, self.host)['round_id'])
        self.assertEqual((started.pk, started.index), (second.pk, 2))
        reveal_round(started, self.host)
        started = Round.objects.get(
            pk=open_round(self.game, self.host)['round_id'])
        self.assertEqual((started.pk, started.index), (third.pk, 3))

    def test_only_hosts_queue_rounds(self):
        with self.assertRaises(PermissionError):
            create_round(self.game, self.player, self.other_question)

    def test_rounds_cannot_be_queued_once_the_game_is_over(self):
        reveal_round(self.round, self.host)
        end_game(self.game, self.host)
        with self.assertRaises(ValueError):
            create_round(self.game, self.host, self.other_question)

class GuessTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])

    def test_a_right_guess_matches_the_title_and_the_artist(self):
        guess = submit_guess(self.round, self.player_row, 'Song', 'Band')
        self.assertTrue(guess.text_correct)
        self.assertTrue(guess.secondary_correct)

    def test_a_wrong_guess_scores_nothing(self):
        guess = submit_guess(self.round, self.player_row, 'Nope', 'Nope')
        self.assertFalse(guess.text_correct)
        self.assertFalse(guess.secondary_correct)

    def test_registered_variants_are_accepted(self):
        AnswerVariant.objects.create(answer=self.answer, text='Song (Remastered)')
        guess = submit_guess(self.round, self.player_row,
                                      'song (remastered)', '')
        self.assertTrue(guess.text_correct)

    def test_a_question_without_secondary_answer_needs_only_the_text(self):
        solo = Question.objects.create(
            expected_answer=Answer.objects.create(text='Paris'))
        self.round.question = solo
        self.round.save(update_fields=['question'])
        guess = submit_guess(self.round, self.player_row, 'Paris', '')
        self.assertTrue(guess.text_correct)
        self.assertFalse(guess.secondary_correct)

    def test_a_guess_stores_the_team_of_the_player(self):
        team = add_team(self.game, self.host, 'Reds', [self.player_row])
        guess = submit_guess(self.round, self.player_row, 'Song', '')
        self.assertEqual(guess.team, team)

    def test_a_guess_without_team_is_solo(self):
        guess = submit_guess(self.round, self.player_row, 'Song', '')
        self.assertIsNone(guess.team)

    def test_no_points_are_stored_on_a_guess(self):
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        fields = [field.name for field in Guess._meta.fields]
        self.assertNotIn('points', fields)

    def test_an_empty_guess_is_refused(self):
        with self.assertRaises(ValueError):
            submit_guess(self.round, self.player_row, '', '   ')

    def test_the_first_guess_is_final(self):
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        with self.assertRaises(ValueError):
            submit_guess(self.round, self.player_row, 'Other', 'Other')

    def test_guessing_a_revealed_round_is_refused(self):
        reveal_round(self.round, self.host)
        with self.assertRaises(ValueError):
            submit_guess(self.round, self.player_row, 'Song', 'Band')

    def test_guessing_an_unstarted_round_is_refused(self):
        queued = Round.objects.get(
            pk=create_round(self.game, self.host, self.other_question)['round_id'])
        with self.assertRaises(ValueError):
            submit_guess(queued, self.player_row, 'Other', '')

class GuessValidationTests(GameTestCase):
    """A host corrects a guess the answer matcher got wrong."""

    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        self.guess = submit_guess(self.round, self.player_row, 'Sng', 'Nope')

    def test_a_host_marks_a_missed_answer_right(self):
        set_guess_correctness(self.guess, self.host, 'text', True)
        self.guess.refresh_from_db()
        self.assertTrue(self.guess.text_correct)
        # The points follow the flag: they are never stored on the guess.
        self.assertEqual(game_scores(self.game)[0]['points'], 1)

    def test_a_host_marks_a_matched_answer_wrong(self):
        set_guess_correctness(self.guess, self.host, 'secondary', True)
        set_guess_correctness(self.guess, self.host, 'secondary', False)
        self.guess.refresh_from_db()
        self.assertFalse(self.guess.secondary_correct)

    def test_each_sub_answer_is_corrected_on_its_own(self):
        set_guess_correctness(self.guess, self.host, 'secondary', True)
        self.guess.refresh_from_db()
        self.assertFalse(self.guess.text_correct)
        self.assertTrue(self.guess.secondary_correct)

    def test_a_plain_member_corrects_nothing(self):
        with self.assertRaises(PermissionError):
            set_guess_correctness(self.guess, self.player, 'text', True)

    def test_a_finished_game_refuses_the_correction(self):
        end_game(self.game, self.host)
        with self.assertRaises(ValueError):
            set_guess_correctness(self.guess, self.host, 'text', True)

    def test_an_unknown_field_is_refused(self):
        with self.assertRaises(ValueError):
            set_guess_correctness(self.guess, self.host, 'artist', True)

    def test_the_lines_list_each_guess_with_its_flags(self):
        [line] = round_guesses(self.round, with_lines=True)['lines']
        self.assertEqual(line['text'], 'Sng')
        self.assertFalse(line['text_correct'])
        self.assertFalse(line['secondary_correct'])
        self.assertTrue(line['player'])
        self.assertEqual(line['team'], '')

    def test_the_counts_alone_do_not_carry_the_guesses(self):
        self.assertNotIn('lines', round_guesses(self.round))


class ScoringTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        self.second = Player.objects.from_discord(FakeMember(44))

    def set_game_scoring_mode(self, mode: str) -> None:
        """Apply a scoring mode to the running game."""
        self.game.scoring_mode = mode
        self.game.save(update_fields=['scoring_mode'])
        self.round.refresh_from_db()

    def test_standard_mode_scores_every_correct_answer(self):
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        submit_guess(self.round, self.second, 'Song', '')
        scores = game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2, 1])

    def test_first_only_mode_scores_the_first_correct_answer(self):
        self.set_game_scoring_mode(ScoringMode.FIRST_ONLY)
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        submit_guess(self.round, self.second, 'Song', 'Band')
        scores = game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2, 0])

    def test_speed_mode_rewards_the_fastest_answers(self):
        self.set_game_scoring_mode(ScoringMode.SPEED)
        third = Player.objects.from_discord(FakeMember(45))
        submit_guess(self.round, self.player_row, 'Song', '')
        submit_guess(self.round, self.second, 'Song', '')
        submit_guess(self.round, third, 'Song', '')
        scores = game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [3, 2, 1])

    def test_the_round_scoring_mode_overrides_the_game_one(self):
        self.set_game_scoring_mode(ScoringMode.FIRST_ONLY)
        self.round.scoring_mode = ScoringMode.STANDARD
        self.round.save(update_fields=['scoring_mode'])
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        submit_guess(self.round, self.second, 'Song', 'Band')
        scores = game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2, 2])

    def test_a_round_inherits_the_game_scoring_mode(self):
        self.set_game_scoring_mode(ScoringMode.FIRST_ONLY)
        self.assertEqual(self.round.effective_scoring_mode, ScoringMode.FIRST_ONLY)
        self.round.scoring_mode = ScoringMode.SPEED
        self.assertEqual(self.round.effective_scoring_mode, ScoringMode.SPEED)

    def test_scores_are_ranked_best_first(self):
        submit_guess(self.round, self.second, 'Song', 'Band')
        submit_guess(self.round, self.player_row, 'Song', '')
        scores = game_scores(self.game)
        self.assertEqual([score['points'] for score in scores], [2, 1])
        self.assertEqual(scores[0]['username'], self.second.username)

class TeamTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        self.teammate = Player.objects.from_discord(FakeMember(44))

    def test_add_team_creates_it_with_its_players(self):
        team = add_team(self.game, self.host, ' Reds ',
                                 [self.player_row])
        self.assertEqual(team.name, 'Reds')
        self.assertEqual(list(team.players.all()), [self.player_row])
        self.assertEqual(team_of(self.game, self.player_row), team)

    def test_a_team_needs_a_name(self):
        with self.assertRaises(ValueError):
            add_team(self.game, self.host, '   ')

    def test_only_hosts_create_teams(self):
        with self.assertRaises(PermissionError):
            add_team(self.game, self.player, 'Reds')

    def test_a_team_name_is_unique_per_game(self):
        add_team(self.game, self.host, 'Reds')
        with self.assertRaises(ValueError):
            add_team(self.game, self.host, 'Reds')

    def test_assigning_a_player_moves_them_between_teams(self):
        reds = add_team(self.game, self.host, 'Reds', [self.player_row])
        blues = add_team(self.game, self.host, 'Blues')
        assign_player(blues, self.host, self.player_row)
        self.assertEqual(list(reds.players.all()), [])
        self.assertEqual(team_of(self.game, self.player_row), blues)

    def test_a_team_takes_the_points_of_its_players(self):
        add_team(self.game, self.host, 'Reds', [self.player_row])
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        rows = game_scores(self.game)
        self.assertEqual([(row['label'], row['points']) for row in rows],
                         [('Reds', 2)])
        # The players points are the team ones, not their own as well.
        self.assertEqual([row['kind'] for row in rows], ['team'])

    def test_team_scores_add_up_their_players_points(self):
        add_team(self.game, self.host, 'Reds', [self.player_row])
        add_team(self.game, self.host, 'Blues', [self.teammate])
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        submit_guess(self.round, self.teammate, 'Song', '')
        rows = game_scores(self.game)
        self.assertEqual([(row['label'], row['points']) for row in rows],
                         [('Reds', 2), ('Blues', 1)])

    def test_a_team_that_did_not_score_is_still_ranked(self):
        add_team(self.game, self.host, 'Reds', [self.player_row])
        submit_guess(self.round, self.player_row, 'Nope', 'Nope')
        self.assertEqual([(row['label'], row['points']) for row in
                          game_scores(self.game)], [('Reds', 0)])

    def test_the_leaderboard_mixes_teams_and_solo_players(self):
        add_team(self.game, self.host, 'Reds', [self.player_row])
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        submit_guess(self.round, self.teammate, 'Song', '')
        rows = game_scores(self.game)
        self.assertEqual([row['kind'] for row in rows], ['team', 'player'])
        self.assertEqual([row['points'] for row in rows], [2, 1])
        self.assertEqual(rows[0]['name'], 'Reds')
        self.assertEqual(rows[1]['username'], self.teammate.username)
    def test_a_guess_line_names_the_team_it_was_made_in(self):
        add_team(self.game, self.host, 'Reds', [self.player_row])
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        [line] = round_guesses(self.round, with_lines=True)['lines']
        self.assertEqual(line['team'], 'Reds')

    def test_the_roster_lists_the_teams_with_their_players(self):
        team = add_team(self.game, self.host, 'Reds', [self.player_row])
        self.assertEqual(teams_of(self.game),
                         [{'pk': team.pk, 'name': 'Reds',
                           'players': [{'pk': self.player_row.pk,
                                         'label': self.player_row.username}]}])

    def test_the_roster_is_forgotten_when_a_team_changes(self):
        team = add_team(self.game, self.host, 'Reds', [self.player_row])
        assign_player(team, self.host, self.teammate)
        self.assertEqual([row['name'] for row in teams_of(self.game)], ['Reds'])
        self.assertEqual(len(teams_of(self.game)[0]['players']), 2)

    def test_a_team_is_found_by_its_id(self):
        team = add_team(self.game, self.host, 'Reds')
        self.assertEqual(team_by_pk(self.game, team.pk), team)
        self.assertEqual(team_by_pk(self.game, f' {team.pk} '), team)

    def test_an_unknown_team_id_is_refused(self):
        with self.assertRaises(ValueError):
            team_by_pk(self.game, 9999)

    def test_a_name_is_not_a_team_id(self):
        add_team(self.game, self.host, 'Reds')
        with self.assertRaises(ValueError):
            team_by_pk(self.game, 'Reds')

    def test_a_team_of_another_game_is_not_found(self):
        other = self._played_team('Blues')
        with self.assertRaises(ValueError):
            team_by_pk(self.create_game(), other.pk)

    def test_only_hosts_rename_or_remove_a_team(self):
        team = add_team(self.game, self.host, 'Reds')
        with self.assertRaises(PermissionError):
            rename_team(team, self.player, 'Blues')
        with self.assertRaises(PermissionError):
            remove_team(team, self.player)

    def test_a_team_name_is_refused_in_another_case(self):
        add_team(self.game, self.host, 'Reds')
        with self.assertRaises(ValueError):
            add_team(self.game, self.host, ' reds ')

    def test_renaming_a_team_keeps_its_own_name(self):
        team = add_team(self.game, self.host, 'Reds')
        rename_team(team, self.host, 'reds ')
        self.assertEqual(team_by_pk(self.game, team.pk).name, 'reds')

    def test_renaming_onto_another_name_is_refused(self):
        add_team(self.game, self.host, 'Reds')
        blues = add_team(self.game, self.host, 'Blues')
        with self.assertRaises(ValueError):
            rename_team(blues, self.host, 'reds')

    def test_a_player_leaves_a_team(self):
        team = add_team(self.game, self.host, 'Reds', [self.player_row])
        remove_player(team, self.host, self.player_row)
        self.assertIsNone(team_of(self.game, self.player_row))

    def test_removing_a_player_outside_the_team_is_refused(self):
        team = add_team(self.game, self.host, 'Reds')
        with self.assertRaises(ValueError):
            remove_player(team, self.host, self.player_row)

    def test_several_players_join_a_team_at_once(self):
        team = add_team(self.game, self.host, 'Reds')
        assign_players(team, self.host, [self.player_row, self.teammate])
        self.assertEqual(len(team.players.all()), 2)

    def test_a_team_that_scored_is_kept(self):
        team = add_team(self.game, self.host, 'Reds', [self.player_row])
        submit_guess(self.round, self.player_row, 'Song', 'Band')
        with self.assertRaises(ValueError):
            remove_team(team, self.host)
        self.assertEqual(team_by_pk(self.game, team.pk).name, 'Reds')

    def test_a_team_without_guesses_is_removed(self):
        team = add_team(self.game, self.host, 'Reds')
        remove_team(team, self.host)
        self.assertEqual(teams_of(self.game), [])

    def test_the_teams_of_a_finished_game_are_frozen(self):
        team = add_team(self.game, self.host, 'Reds')
        end_game(self.game, self.host)
        self.game.refresh_from_db()
        team.refresh_from_db()
        for refused in (lambda: add_team(self.game, self.host, 'Blues'),
                       lambda: rename_team(team, self.host, 'Blues'),
                       lambda: remove_team(team, self.host),
                       lambda: assign_player(team, self.host, self.teammate),
                       lambda: assign_players(team, self.host, [self.teammate]),
                       lambda: remove_player(team, self.host, self.player_row)):
            with self.subTest(refused=refused):
                with self.assertRaises(ValueError):
                    refused()

    def test_a_team_is_copied_with_its_players_into_another_game(self):
        source = self._played_team('Reds', [self.player_row, self.teammate])
        game = self.create_game()
        copied = copy_team(game, self.host, source)
        self.assertEqual(copied.name, 'Reds')
        self.assertEqual(list(copied.players.all()),
                         [self.player_row, self.teammate])
        self.assertEqual(list(source.players.all()),
                         [self.player_row, self.teammate])

    def test_a_copy_is_refused_a_name_the_game_already_has(self):
        source = self._played_team('Reds')
        game = self.create_game()
        add_team(game, self.host, 'Reds')
        with self.assertRaises(ValueError):
            copy_team(game, self.host, source)
        self.assertEqual(game.teams.count(), 1)

    def test_copying_a_team_of_the_game_itself_is_refused(self):
        source = add_team(self.game, self.host, 'Reds')
        with self.assertRaises(ValueError):
            copy_team(self.game, self.host, source)

    def test_copying_a_team_of_another_server_is_refused(self):
        add_host(self.other_guild, user_mention(self.host.id), self.admin)
        source = add_team(self.create_game(self.other_guild), self.host, 'Reds')
        with self.assertRaises(ValueError):
            copy_team(self.game, self.host, source)

    def test_only_hosts_copy_a_team(self):
        source = self._played_team('Reds')
        game = self.create_game()
        with self.assertRaises(PermissionError):
            copy_team(game, self.player, source)

    def test_a_finished_game_takes_no_copied_team(self):
        source = self._played_team('Reds')
        game = self.create_game()
        end_game(game, self.host)
        with self.assertRaises(ValueError):
            copy_team(game, self.host, source)

    def test_a_team_that_vanished_is_reported(self):
        with self.assertRaises(ValueError):
            copy_team_by_pk(self.game, self.host, 999)

    def test_the_teams_to_copy_are_those_of_the_other_games(self):
        source = self._played_team('Reds')
        game = self.create_game()
        add_team(game, self.host, 'Blues')
        self.assertEqual([choice['pk']
                          for choice in copyable_teams(self.guild, game)],
                         [source.pk])

    def test_the_teams_to_copy_are_named_after_the_game_they_played_in(self):
        past = self.game
        past.name = 'Friday quiz'
        past.save(update_fields=['name'])
        self._played_team('Reds')
        self.assertEqual(copyable_teams(self.guild, self.create_game())[0]
                         ['label'], 'Friday quiz — Reds')

    def test_the_teams_to_copy_are_searched_by_game_and_by_name(self):
        past = self.game
        past.name = 'Friday quiz'
        past.save(update_fields=['name'])
        add_team(past, self.host, 'Reds')
        add_team(past, self.host, 'Blues')
        end_game(past, self.host)
        game = self.create_game()
        for term, names in (('Red', ['Reds']), ('Friday', ['Blues', 'Reds']),
                            ('blues', ['Blues'])):
            with self.subTest(term=term):
                labels = [choice['label'] for choice in
                          copyable_teams(self.guild, game, term)]
                self.assertEqual(sorted(label.rsplit(' — ', 1)[1]
                                        for label in labels), names)

    def test_the_teams_of_another_server_are_never_offered_to_copy(self):
        add_host(self.other_guild, user_mention(self.host.id), self.admin)
        add_team(self.create_game(self.other_guild), self.host, 'Reds')
        self.assertEqual(copyable_teams(self.guild, self.game), [])

    def _played_team(self, name: str,
                     players: list = ()) -> Team:
        """Return a team of a game of this server that has been played and ended.

        The game this test case is already playing is the one that ends, so the
        tests after it start from a server with no game running.
        """
        team = add_team(self.game, self.host, name, players)
        end_game(self.game, self.host)
        self.game.refresh_from_db()
        return team

    def test_the_permission_is_checked_before_the_game_state(self):
        end_game(self.game, self.host)
        self.game.refresh_from_db()
        with self.assertRaises(PermissionError):
            add_team(self.game, self.player, 'Reds')


class MatchingTests(NoNetworkMixin, SimpleTestCase):
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
        host = add_host(self.guild, user_mention(50), self.admin)
        self.assertEqual(host.mention, '<@50>')
        self.assertIn('<@50>', hosts_of(self.guild))

    def test_adding_a_host_twice_changes_nothing(self):
        first = add_host(self.guild, user_mention(50), self.admin)
        second = add_host(self.guild, user_mention(50), self.admin)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.guild.hosts.filter(mention='<@50>').count(), 1)

    def test_only_admins_manage_hosts(self):
        with self.assertRaises(PermissionError):
            add_host(self.guild, user_mention(50), self.player)

    def test_an_admin_removes_a_host(self):
        add_host(self.guild, user_mention(50), self.admin)
        remove_host(self.guild, user_mention(50), self.admin)
        self.assertNotIn('<@50>', hosts_of(self.guild))

    def test_removing_an_unknown_host_is_refused(self):
        with self.assertRaises(ValueError):
            remove_host(self.guild, user_mention(50), self.admin)

    def test_only_admins_remove_hosts(self):
        add_host(self.guild, user_mention(50), self.admin)
        with self.assertRaises(PermissionError):
            remove_host(self.guild, user_mention(50), self.player)

    def test_adding_a_host_that_is_not_a_mention_is_refused(self):
        with self.assertRaises(ValueError) as refused:
            add_host(self.guild, 'not a mention', self.admin)
        self.assertIn('Enter a Discord mention', str(refused.exception))
        self.assertEqual(self.guild.hosts.count(), 1)

    def test_removing_a_host_that_is_not_a_mention_is_refused(self):
        with self.assertRaises(ValueError) as refused:
            remove_host(self.guild, 'not a mention', self.admin)
        self.assertIn('Enter a Discord mention', str(refused.exception))
        self.assertIn('<@42>', hosts_of(self.guild))


class LibraryRemovalTests(GameTestCase):
    """Removing what no game played and no question uses."""

    def setUp(self):
        super().setUp()
        self.own_answer = self.guild.answers.create(text='Own')
        self.question = self.guild.questions.create(
            expected_answer=self.own_answer, author=self.host_player)
        add_host(self.other_guild, user_mention(42),
                          FakeMember(42, manage_guild=True))

    def test_a_host_removes_an_unplayed_question(self):
        remove_question(self.guild, self.host, self.question.pk)
        self.assertFalse(Question.objects.filter(pk=self.question.pk).exists())

    def test_a_played_question_is_kept(self):
        game = self.create_game()
        open_round(game, self.host, self.question)
        with self.assertRaises(ValueError) as refused:
            remove_question(self.guild, self.host, self.question.pk)
        self.assertIn('played in a game', str(refused.exception))
        self.assertTrue(Question.objects.filter(pk=self.question.pk).exists())

    def test_removing_a_question_of_another_guild_is_refused(self):
        with self.assertRaises(ValueError):
            remove_question(self.other_guild, self.host,
                                     self.question.pk)

    def test_a_host_removes_an_unused_answer(self):
        spare = self.guild.answers.create(text='Spare')
        remove_answer(self.guild, self.host, spare.pk)
        self.assertFalse(self.guild.answers.filter(pk=spare.pk).exists())

    def test_an_answer_a_question_uses_is_kept(self):
        with self.assertRaises(ValueError) as refused:
            remove_answer(self.guild, self.host, self.own_answer.pk)
        self.assertIn('used by the question', str(refused.exception))
        self.assertTrue(
            self.guild.answers.filter(pk=self.own_answer.pk).exists())

    def test_the_refusal_names_the_question_that_uses_the_answer(self):
        self.question.prompt = 'Guess it'
        self.question.save(update_fields=['prompt'])
        with self.assertRaises(ValueError) as refused:
            remove_answer(self.guild, self.host, self.own_answer.pk)
        self.assertIn('Guess it', str(refused.exception))

    def test_an_unplayed_question_keeps_its_answer(self):
        with self.assertRaises(ValueError):
            remove_answer(self.guild, self.host, self.own_answer.pk)
        self.assertTrue(Question.objects.filter(pk=self.question.pk).exists())

    def test_removing_an_answer_of_another_guild_is_refused(self):
        with self.assertRaises(ValueError):
            remove_answer(self.other_guild, self.host,
                                   self.own_answer.pk)

    def test_a_stranger_removes_nothing(self):
        with self.assertRaises(PermissionError):
            remove_question(self.guild, self.player, self.question.pk)
        with self.assertRaises(PermissionError):
            remove_answer(self.guild, self.player, self.own_answer.pk)

    def test_the_unused_lists_name_what_can_go(self):
        self.assertEqual(
            [row['pk'] for row in unused_questions(self.guild, self.host_player)],
            [self.question.pk])
        self.assertEqual(unused_answers(self.guild), [])

    def test_a_played_question_leaves_the_unused_list(self):
        game = self.create_game()
        open_round(game, self.host, self.question)
        self.assertEqual(unused_questions(self.guild, self.host_player), [])
        self.assertEqual(unused_answers(self.guild), [])

    def test_a_removed_question_leaves_its_answer_behind(self):
        remove_question(self.guild, self.host, self.question.pk)
        self.assertEqual(unused_questions(self.guild, self.host_player), [])
        self.assertEqual(
            [answer['text'] for answer in unused_answers(self.guild)],
            ['Own'])
        remove_answer(self.guild, self.host, self.own_answer.pk)
        self.assertEqual(unused_answers(self.guild), [])

    def test_the_host_replaces_the_variants_of_an_answer(self):
        set_variants(self.guild, self.host, 'Own', ['Tune', 'Air'])
        self.assertEqual(
            [variant.text for variant in self.own_answer.variants.all()],
            ['Air', 'Tune'])
        set_variants(self.guild, self.host, 'Own', ['Tune'])
        self.assertEqual(
            [variant.text for variant in self.own_answer.variants.all()],
            ['Tune'])

    def test_the_host_removes_every_variant_of_an_answer(self):
        set_variants(self.guild, self.host, 'Own', ['Tune'])
        set_variants(self.guild, self.host, 'Own', [])
        self.assertEqual(self.own_answer.variants.count(), 0)

    def test_the_variants_of_an_answer_that_is_not_ours_are_refused(self):
        with self.assertRaises(ValueError):
            set_variants(self.guild, self.host, 'Missing', ['Tune'])


class LibraryTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.other_question.delete()
        self.game = self.create_game()
        self.round = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])

    def test_add_answer_creates_it_for_the_guild(self):
        answer = add_answer(self.guild, self.host, ' Wonderwall ')
        self.assertEqual(answer.text, 'Wonderwall')
        self.assertEqual(answer.guild, self.guild)

    def test_add_answer_is_idempotent(self):
        first = add_answer(self.guild, self.host, 'Wonderwall')
        second = add_answer(self.guild, self.host, 'wonderwall')
        self.assertEqual(first.pk, second.pk)

    def test_only_hosts_add_answers(self):
        with self.assertRaises(PermissionError):
            add_answer(self.guild, self.player, 'Nope')

    def test_an_answer_needs_a_text(self):
        with self.assertRaises(ValueError):
            add_answer(self.guild, self.host, '   ')

    def test_add_question_creates_it_for_the_guild(self):
        question = Question.objects.get(
            pk=add_question(
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
            pk=add_question(self.guild, self.host, 'Wonderwall',
                                     media_url='https://youtu.be/1')['pk'])
        self.assertEqual(question.media_url, 'https://youtu.be/1')

    def test_a_media_link_must_be_a_full_url(self):
        for url in ('youtu.be/1', 'www.youtube.com/watch?v=1', ' song.mp3'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    add_question(self.guild, self.host, 'Wonderwall',
                                          media_url=url)
        self.assertFalse(Answer.objects.filter(text='Wonderwall').exists())

    def test_a_year_the_column_cannot_keep_is_refused(self):
        for year in (0, 10000):
            with self.subTest(year=year):
                with self.assertRaises(ValueError):
                    add_question(self.guild, self.host, 'Wonderwall',
                                          year=year)
        self.assertFalse(Answer.objects.filter(text='Wonderwall').exists())

    def test_only_hosts_add_questions(self):
        with self.assertRaises(PermissionError):
            add_question(self.guild, self.player, 'Song')

    def test_a_question_needs_an_answer(self):
        with self.assertRaises(ValueError):
            add_question(self.guild, self.host, '   ')

    def test_a_question_can_be_created_with_choices(self):
        question = Question.objects.get(
            pk=add_question(
                                     self.guild, self.host, 'Right', prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        self.assertEqual([choice.text for choice in question.choices.all()],
                         ['Right', 'Wrong'])

    def test_choices_without_a_prompt_are_refused(self):
        with self.assertRaises(ValueError):
            add_question(self.guild, self.host, 'Right',
                                  choices=['Right', 'Wrong'])

    def test_the_expected_answer_must_be_a_choice(self):
        with self.assertRaises(ValueError):
            add_question(self.guild, self.host, 'Right', prompt='Pick one',
                                  choices=['Wrong', 'Other'])

    def test_a_refused_choice_question_leaves_nothing_behind(self):
        with self.assertRaises(ValueError):
            add_question(self.guild, self.host, 'Right',
                                  choices=['Right', 'Wrong'])
        self.assertFalse(Answer.objects.filter(text='Right').exists())
        self.assertFalse(Question.objects.filter(prompt='').filter(
            expected_answer__text='Right').exists())

    def test_add_question_returns_a_plain_label(self):
        result = add_question(
            self.guild, self.host, 'Song', secondary_text='Band')
        self.assertIsInstance(result['pk'], int)
        self.assertEqual(result['label'], 'Song (Band)')

    def test_add_question_records_the_host_that_authored_it(self):
        question = Question.objects.get(
            pk=add_question(self.guild, self.host, 'Song')['pk'])
        self.assertEqual(question.author, self.host_player)

    def test_question_choices_carry_plain_labels(self):
        Question.objects.create(
            guild=self.guild, author=self.host_player,
            expected_answer=Answer.objects.create(text='Morning Bell'),
            secondary_answer=Answer.objects.create(text='Radiohead'))
        [choice] = question_choices(self.game, self.host_player)
        self.assertEqual(choice['label'], 'Morning Bell (Radiohead)')


    def test_question_choices_offer_global_and_guild_questions(self):
        global_question = Question.objects.create(
            author=self.host_player,
            expected_answer=Answer.objects.create(text='Global'))
        own = Question.objects.create(
            guild=self.guild, author=self.host_player,
            expected_answer=Answer.objects.create(text='Own'))
        Question.objects.create(
            guild=self.other_guild,
            expected_answer=Answer.objects.create(text='Foreign'))
        choices = question_choices(self.game, self.host_player)
        self.assertEqual({choice['pk'] for choice in choices},
                         {global_question.pk, own.pk})

    def test_question_choices_skip_played_questions(self):
        self.assertEqual(question_choices(self.game, self.host_player), [])

    def test_question_choices_search_prompt_and_answers(self):
        own = Question.objects.create(
            guild=self.guild, prompt='Guess the album', author=self.host_player,
            expected_answer=Answer.objects.create(text='OK Computer'))
        Question.objects.create(
            expected_answer=Answer.objects.create(text='Wonderwall'))
        found = question_choices(self.game, self.host_player, 'album')
        self.assertEqual([choice['pk'] for choice in found], [own.pk])
        found = question_choices(self.game, self.host_player, 'computer')
        self.assertEqual([choice['pk'] for choice in found], [own.pk])

    def test_pick_question_draws_a_global_question(self):
        global_question = Question.objects.create(
            expected_answer=Answer.objects.create(text='Global'))
        self.assertEqual(pick_question(self.game).pk,
                         global_question.pk)

    def test_pick_question_draws_the_guilds_own_question(self):
        own = Question.objects.create(
            guild=self.guild, expected_answer=Answer.objects.create(text='Own'))
        self.assertEqual(pick_question(self.game).pk, own.pk)

    def test_pick_question_never_draws_another_guilds_question(self):
        Question.objects.create(
            guild=self.other_guild,
            expected_answer=Answer.objects.create(text='Foreign'))
        with self.assertRaises(ValueError):
            pick_question(self.game)

    def test_a_round_cannot_use_another_guilds_question(self):
        foreign = Question.objects.create(
            guild=self.other_guild,
            expected_answer=Answer.objects.create(text='Foreign'))
        with self.assertRaises(ValueError):
            create_round(self.game, self.host, foreign)
        reveal_round(self.round, self.host)
        with self.assertRaises(ValueError):
            open_round(self.game, self.host, foreign)


class QuestionEditTests(GameTestCase):
    """A host changes the fields of a question in one go, its answers too."""

    def setUp(self):
        super().setUp()
        self.own = Question.objects.get(
            pk=add_question(
                self.guild, self.host, 'Song', prompt='Guess it',
                secondary_text='Band', year=1999, album='Album',
                media_url='https://youtu.be/1')['pk'])

    def edit(self, field, value=None, question=None):
        """Change one field of a question and return it reloaded."""
        question = question or self.own
        edit_question(self.guild, self.host, question.pk,
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
            edit_question(self.guild, self.player, self.own.pk,
                                   album='Album')
        with self.assertRaises(PermissionError):
            editable_question(self.guild, self.player, self.own.pk)

    def test_several_fields_change_in_one_call(self):
        edit_question(self.guild, self.host, self.own.pk,
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
            edit_question(self.guild, self.host, self.own.pk)
        self.own.refresh_from_db()
        self.assertEqual(self.own.prompt, 'Guess it')

    def test_the_answers_and_the_choices_change_together(self):
        question = Question.objects.get(
            pk=add_question(self.guild, self.host, 'Right',
                                     prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        edit_question(self.guild, self.host, question.pk,
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
            editable_question(self.guild, self.host, foreign.pk)

    def test_a_global_question_is_not_editable(self):
        with self.assertRaises(ValueError):
            editable_question(self.guild, self.host,
                                       self.question.pk)

    def _question_of_another_host(self, author=None) -> Question:
        """Return a question of this server another host authored."""
        add_host(self.guild, user_mention(43), self.admin)
        return Question.objects.create(
            guild=self.guild,
            expected_answer=Answer.objects.create(text='Theirs'),
            author=author)

    def test_a_host_does_not_change_the_question_of_another_host(self):
        theirs = self._question_of_another_host(self.player_row)
        with self.assertRaises(PermissionError):
            edit_question(self.guild, self.host, theirs.pk,
                                   album='Album')
        theirs.refresh_from_db()
        self.assertEqual(theirs.album, '')

    def test_a_host_does_not_change_a_question_nobody_authored(self):
        theirs = self._question_of_another_host()
        with self.assertRaises(PermissionError):
            editable_question(self.guild, self.host, theirs.pk)

    def test_an_administrator_changes_the_question_of_another_host(self):
        theirs = self._question_of_another_host(self.player_row)
        edit_question(self.guild, self.admin, theirs.pk,
                               album='Album')
        theirs.refresh_from_db()
        self.assertEqual(theirs.album, 'Album')

    def test_an_administrator_changes_a_question_nobody_authored(self):
        theirs = self._question_of_another_host()
        edit_question(self.guild, self.admin, theirs.pk,
                               album='Album')
        theirs.refresh_from_db()
        self.assertEqual(theirs.album, 'Album')

    def test_a_host_does_not_remove_the_question_of_another_host(self):
        theirs = self._question_of_another_host(self.player_row)
        with self.assertRaises(PermissionError):
            remove_question(self.guild, self.host, theirs.pk)
        self.assertTrue(Question.objects.filter(pk=theirs.pk).exists())

    def test_a_question_that_is_gone_is_reported(self):
        for pk in (self.own.pk + 100, 'soon'):
            with self.subTest(pk=pk):
                with self.assertRaises(ValueError):
                    editable_question(self.guild, self.host, pk)

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
            pk=add_question(self.guild, self.host, 'Wonderwall')['pk'])
        self.edit('answer', 'wonderwall')
        self.assertEqual(self.own.expected_answer, other.expected_answer)

    def test_renaming_an_answer_leaves_the_questions_using_it_alone(self):
        other = Question.objects.get(
            pk=add_question(self.guild, self.host, 'Wonderwall')['pk'])
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
            pk=add_question(self.guild, self.host, 'Right',
                                     prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        self.edit('answer', 'Righter', question=question)
        self.assertEqual(sorted(choice.text
                                for choice in question.choices.all()),
                         ['Righter', 'Wrong'])

    def test_the_choices_are_replaced_and_removed(self):
        question = Question.objects.get(
            pk=add_question(self.guild, self.host, 'Right',
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
            pk=add_question(self.guild, self.host, 'Right',
                                     prompt='Pick one',
                                     choices=['Right', 'Wrong'])['pk'])
        with self.assertRaises(ValueError):
            self.edit('choices', 'Wrong, Other', question=question)
        self.assertEqual(sorted(choice.text
                                for choice in question.choices.all()),
                         ['Right', 'Wrong'])

    def test_a_queued_round_refuses_an_edit_it_cannot_play(self):
        game = self.create_game()
        create_round(game, self.host, self.own, QuizType.OPEN)
        with self.assertRaises(ValueError):
            edit_question(self.guild, self.host, self.own.pk,
                                   prompt='')
        self.own.refresh_from_db()
        self.assertEqual(self.own.prompt, 'Guess it')

    def test_a_batch_a_queued_round_refuses_leaves_every_field_alone(self):
        game = self.create_game()
        create_round(game, self.host, self.own, QuizType.OPEN)
        with self.assertRaises(ValueError):
            edit_question(self.guild, self.host, self.own.pk,
                                   album='Other album', prompt='')
        self.own.refresh_from_db()
        self.assertEqual(self.own.prompt, 'Guess it')
        self.assertEqual(self.own.album, 'Album')

    def test_a_queued_blind_test_round_leaves_a_prompt_optional(self):
        game = self.create_game()
        create_round(game, self.host, self.own)
        self.edit('prompt', '')
        self.assertEqual(self.own.prompt, '')

    def test_edit_question_returns_a_plain_label(self):
        result = edit_question(
            self.guild, self.host, str(self.own.pk), prompt='Guess the title')
        self.assertEqual(result['pk'], self.own.pk)
        self.assertEqual(result['fields'], ['prompt'])
        self.assertEqual(result['label'],
                         'Guess the title — answer: Song (Band)')
        self.assertEqual(result['media_url'], 'https://youtu.be/1')
        self.assertEqual(result['choices'], 0)

    def test_edit_question_lists_the_fields_it_changed(self):
        result = edit_question(
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
            [choice['pk'] for choice in library_choices(self.guild, self.host_player)],
            [self.own.pk])

    def test_library_choices_carry_the_label_and_the_media(self):
        [choice] = library_choices(self.guild, self.host_player)
        self.assertEqual(choice['label'], 'Guess it — answer: Song (Band)')
        self.assertTrue(choice['media'])

    def test_library_choices_search_the_prompt_and_the_answers(self):
        self.assertEqual(
            [choice['pk']
             for choice in library_choices(self.guild, self.host_player,
                                           'band')],
            [self.own.pk])
        self.assertEqual(library_choices(self.guild, self.host_player,
                                         'unknown'), [])


class QuestionSpoilerTests(GameTestCase):
    """A host reads their own questions, and the ones they asked to spoil."""

    def setUp(self):
        super().setUp()
        self.game = self.create_game()
        # A second host of the server, and what they authored.
        add_host(self.guild, user_mention(43), self.admin)
        self.mine = Question.objects.create(
            guild=self.guild,
            expected_answer=Answer.objects.create(text='Mine'),
            author=self.host_player)
        self.theirs = Question.objects.create(
            guild=self.guild,
            expected_answer=Answer.objects.create(text='Theirs'),
            author=self.player_row)
        self.anonymous = Question.objects.create(
            guild=self.guild,
            expected_answer=Answer.objects.create(text='Anonymous'))

    def _read_pks(self) -> set[int]:
        """Return the questions of the server's library a host may read."""
        return {row['pk'] for row in own_questions(self.guild, self.host_player)}

    def test_a_host_reads_their_own_questions(self):
        self.assertEqual(self._read_pks(), {self.mine.pk})

    def test_a_host_reads_no_question_of_another_host(self):
        self.assertNotIn(self.theirs.pk, self._read_pks())

    def test_a_question_nobody_authored_is_hidden_like_any_other(self):
        self.assertNotIn(self.anonymous.pk, self._read_pks())

    def test_the_picker_offers_no_question_of_another_host(self):
        self.assertEqual(
            {choice['pk']
             for choice in library_choices(self.guild, self.host_player)},
            {self.mine.pk})

    def test_the_game_picker_offers_no_question_of_another_host(self):
        offered = {choice['pk']
                   for choice in question_choices(self.game, self.host_player)}
        self.assertIn(self.mine.pk, offered)
        self.assertNotIn(self.theirs.pk, offered)
        self.assertNotIn(self.anonymous.pk, offered)

    def test_a_caller_without_a_viewer_offers_nothing(self):
        # Naming no viewer is not the same as asking for every question.
        self.assertEqual(question_choices(self.game, None), [])
        self.assertEqual(library_choices(self.guild, None), [])
        self.assertEqual(own_questions(self.guild, None), [])

    def test_asking_for_every_question_shows_them_on_the_cached_picker(self):
        # A library's options are cached once for the server, so the viewer's
        # choice is what filters the cached list, not a second read.
        library_choices(self.guild, self.host_player)
        set_show_all_questions(self.host_player, True)
        with self.assertNumQueries(0):
            offered = {choice['pk'] for choice
                       in library_choices(self.guild, self.host_player)}
        self.assertEqual(offered,
                         {self.mine.pk, self.theirs.pk, self.anonymous.pk})

    def test_every_question_of_the_server_is_then_readable(self):
        set_show_all_questions(self.host_player, True)
        self.assertEqual(self._read_pks(),
                         {self.mine.pk, self.theirs.pk, self.anonymous.pk})
        self.assertEqual(len(unused_questions(self.guild, self.host_player)), 3)

    def test_the_choice_lasts_on_the_player_row(self):
        set_show_all_questions(self.host_player, False)
        self.host_player.refresh_from_db()
        self.assertFalse(self.host_player.show_all_questions)

    def test_a_host_may_queue_a_question_they_do_not_read(self):
        # Seeing a question is not what allows a game to play it.
        queue_questions(self.game, self.host, [self.theirs.pk])
        self.assertEqual(
            [round_.question_id for round_ in self.game.rounds.all()],
            [self.theirs.pk])


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
        auto = create_game(self.other_guild, 100, self.admin,
                                   quiz_type=QuizType.MULTIPLE_CHOICE)
        self.assertEqual(auto.display_name, f'Multiple choice #{auto.pk}')

    def test_a_named_game_keeps_its_name(self):
        game = create_game(self.other_guild, 100, self.admin,
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
            pk=open_round(self.game, self.host, self.question)['round_id'])
        self.assertEqual(round_.type, '')
        self.assertEqual(round_.effective_type, QuizType.BLIND_TEST)

    def test_a_round_can_override_the_game_type(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host,
                                    self._choice_question(),
                                    QuizType.MULTIPLE_CHOICE)['round_id'])
        self.assertEqual(round_.type, QuizType.MULTIPLE_CHOICE)
        self.assertEqual(round_.effective_type, QuizType.MULTIPLE_CHOICE)

    def test_an_open_round_needs_a_prompted_question(self):
        with self.assertRaises(ValueError):
            open_round(self.game, self.host, self.question, QuizType.OPEN)

    def test_a_multiple_choice_round_needs_two_choices(self):
        expected = Answer.objects.create(text='Alone')
        question = Question.objects.create(prompt='Pick one',
                                           expected_answer=expected)
        question.choices.set([expected])
        with self.assertRaises(ValueError):
            create_round(self.game, self.host, question,
                                  QuizType.MULTIPLE_CHOICE)

    def test_the_expected_answer_must_be_one_of_the_choices(self):
        question = self._prompted_question()
        question.choices.set([Answer.objects.create(text='One'),
                              Answer.objects.create(text='Two')])
        with self.assertRaises(ValueError):
            create_round(self.game, self.host, question,
                                  QuizType.MULTIPLE_CHOICE)

    def test_a_blind_test_round_plays_a_question_without_a_prompt(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        self.assertEqual(round_display(round_)['prompt'],
                         DEFAULT_BLIND_TEST_PROMPT)

    def test_an_open_round_shows_the_prompt_of_its_question(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host,
                                    self._prompted_question(), QuizType.OPEN)['round_id'])
        self.assertEqual(round_display(round_)['prompt'], 'Which album?')

    def test_a_draw_skips_questions_the_round_type_cannot_play(self):
        with self.assertRaises(ValueError):
            open_round(self.game, self.host, quiz_type=QuizType.OPEN)
        prompted = self._prompted_question()
        started = Round.objects.get(
            pk=open_round(self.game, self.host,
                                    quiz_type=QuizType.OPEN)['round_id'])
        self.assertEqual(started.question, prompted)

    def test_a_draw_finds_a_multiple_choice_question_with_choices(self):
        question = self._choice_question()
        started = Round.objects.get(
            pk=open_round(self.game, self.host,
                                    quiz_type=QuizType.MULTIPLE_CHOICE)['round_id'])
        self.assertEqual(started.question, question)

    def test_the_form_of_a_text_round_offers_no_option(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        self.assertEqual(guess_form(round_display(round_))['options'], [])

    def test_the_form_of_a_multiple_choice_round_offers_its_choices(self):
        round_ = Round.objects.get(
            pk=create_round(self.game, self.host,
                                     self._choice_question(),
                                     QuizType.MULTIPLE_CHOICE)['round_id'])
        form = guess_form(round_display(round_))
        self.assertEqual(form['type'], QuizType.MULTIPLE_CHOICE)
        self.assertEqual([option['label'] for option in form['options']],
                         ['Right', 'Wrong'])

    def test_the_round_counts_the_queued_questions(self):
        create_round(self.game, self.host, self.other_question)
        later = Question.objects.create(
            expected_answer=Answer.objects.create(text='Later'))
        create_round(self.game, self.host, later)
        result = open_round(self.game, self.host, self.question)
        self.assertEqual(result['queued'], 1)
        self.assertEqual(result['game_name'], self.game.display_name)
        self.assertEqual(result['type_label'], 'Blind test')

    def test_the_rounds_carry_the_media_link(self):
        self.other_question.media_url = 'https://youtu.be/1'
        self.other_question.save(update_fields=['media_url'])
        queued = create_round(self.game, self.host, self.other_question)
        started = open_round(self.game, self.host, self.question)
        self.assertEqual(queued['media_url'], 'https://youtu.be/1')
        self.assertEqual(started['media_url'], '')

    def test_the_reveal_counts_the_answers_and_the_right_ones(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        submit_guess(round_, self.player_row, 'Song', 'Band')
        result = reveal_round(round_, self.host)
        self.assertEqual(result['answer_text'], 'Song (Band)')
        self.assertEqual((result['guessed'], result['right']), (1, 1))
        self.assertEqual(result['right_names'], ['user43'])

    def test_the_recap_summarises_the_game(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        submit_guess(round_, self.player_row, 'Song', 'Band')
        reveal_round(round_, self.host)
        recap = end_game(self.game, self.host)
        self.assertEqual(recap['rounds'], 1)
        self.assertEqual(recap['guesses'], 1)
        self.assertEqual(recap['game_name'], self.game.display_name)
        self.assertEqual([row['points'] for row in recap['scores']], [2])

    def test_a_queued_question_is_not_a_round_played(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        submit_guess(round_, self.player_row, 'Song', 'Band')
        reveal_round(round_, self.host)
        create_round(self.game, self.host, self.other_question)
        recap = end_game(self.game, self.host)
        self.assertEqual(recap['rounds'], 1)
        self.assertEqual(recap['guesses'], 1)
        self.assertEqual([row['points'] for row in recap['scores']], [2])

    def test_a_round_edited_in_the_admin_is_validated(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host, self.question)['round_id'])
        round_.type = QuizType.MULTIPLE_CHOICE
        with self.assertRaises(ValidationError):
            round_.full_clean()

    def test_the_question_line_shows_the_answer_only_to_a_host(self):
        prompted = self._prompted_question()
        self.assertEqual(question_line(self.question), 'Song (Band)')
        self.assertEqual(question_line(self.question, with_answer=True),
                         'Song (Band)')
        self.assertEqual(question_line(prompted), 'Which album?')
        self.assertEqual(
            question_line(prompted, with_answer=True),
            'Which album? — answer: Album')

    def test_a_question_never_shows_its_answer_when_stringified(self):
        self.assertEqual(str(self.question), f'Question #{self.question.pk}')
        self.assertEqual(str(self._prompted_question()), 'Which album?')
        self.assertNotIn('Song', str(self.question))

    def test_the_host_text_of_a_round_carries_the_answer(self):
        round_ = Round.objects.get(
            pk=open_round(self.game, self.host,
                                    self._prompted_question(), QuizType.OPEN)['round_id'])
        display = round_display(round_)
        self.assertEqual(display['question_text'], 'Which album?')
        self.assertEqual(display['host_text'], 'Which album? — answer: Album')


class SetupStateTests(GameTestCase):
    """A game is prepared in SETUP, then published to run."""

    def setup(self, **kwargs) -> dict:
        """Create a game awaiting publication and return its panel data."""
        game = create_game(self.guild, 100, self.host,
                                    state=Game.State.SETUP, **kwargs)
        return panel_data(game, self.host_player)

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
            open_round(game, self.host, self.question)

    def test_publishing_starts_the_game(self):
        game = self.game_in_setup(name='Fiesta')
        published = publish_game(game, self.host)
        game.refresh_from_db()
        self.assertTrue(game.is_running)
        self.assertEqual(published['game_name'], 'Fiesta')
        self.assertEqual(published['queued'], 0)
        self.assertEqual(published['type_label'], 'Blind test')

    def test_publishing_twice_is_refused(self):
        game = self.game_in_setup()
        publish_game(game, self.host)
        with self.assertRaises(ValueError):
            publish_game(game, self.host)

    def test_only_hosts_publish(self):
        game = self.game_in_setup()
        with self.assertRaises(PermissionError):
            publish_game(game, self.player)

    def test_closing_an_unpublished_game_finishes_it(self):
        game = self.game_in_setup()
        end_game(game, self.host)
        game.refresh_from_db()
        self.assertEqual(game.state, Game.State.FINISHED)
        self.assertFalse(game.is_preparing)


class QueueTests(GameTestCase):
    """The setup panel queues, removes and copies questions."""

    def test_queueing_questions_counts_what_was_added(self):
        game = self.create_game()
        result = queue_questions(
            game, self.host, [self.question.pk, self.other_question.pk])
        self.assertEqual((result['added'], result['skipped']), (2, 0))
        self.assertEqual(result['queued'], 2)

    def test_only_hosts_queue_questions(self):
        game = self.create_game()
        with self.assertRaises(PermissionError):
            queue_questions(game, self.player, [self.question.pk])

    def test_clearing_the_queue_removes_every_queued_question(self):
        game = self.create_game()
        queue_questions(game, self.host, [self.question.pk])
        self.assertEqual(clear_queue(game, self.host), 1)
        self.assertEqual(queued_count(game), 0)

    def test_copying_a_game_queues_its_questions(self):
        source = self.create_game()
        open_round(source, self.host, self.question)
        end_game(source, self.host)
        game = self.create_game()
        result = copy_questions_by_pk(game, self.host, source.pk)
        self.assertEqual((result['added'], result['skipped']), (1, 0))
        self.assertEqual(result['queued'], 1)

    def test_copying_the_game_itself_is_refused(self):
        game = self.create_game()
        with self.assertRaises(ValueError):
            copy_questions_by_pk(game, self.host, game.pk)

    def test_copying_a_game_of_another_server_is_refused(self):
        foreign = create_game(self.other_guild, 100, self.admin)
        open_round(foreign, self.admin, self.question)
        end_game(foreign, self.admin)
        game = self.create_game()
        with self.assertRaises(ValueError):
            copy_questions_by_pk(game, self.host, foreign.pk)


class VariantTests(GameTestCase):
    """Answers accept the variant texts hosts register."""

    def test_split_answers_separates_the_variants(self):
        self.assertEqual(split_answers('Song | Song (Remastered)'),
                         ('Song', ['Song (Remastered)']))
        self.assertEqual(split_answers('  Song '), ('Song', []))
        self.assertEqual(split_answers(''), ('', []))

    def test_add_variant_registers_the_text(self):
        add_variant(self.guild, self.host, 'Song', 'Song (Remastered)')
        self.assertEqual(variants_of(self.guild, self.host, 'Song'),
                         ['Song (Remastered)'])

    def test_adding_the_same_variant_twice_is_refused(self):
        add_variant(self.guild, self.host, 'Song', 'Remix')
        with self.assertRaises(ValueError):
            add_variant(self.guild, self.host, 'Song', 'remix')

    def test_the_answer_itself_is_not_a_variant(self):
        with self.assertRaises(ValueError):
            add_variant(self.guild, self.host, 'Song', 'song')

    def test_only_hosts_manage_variants(self):
        with self.assertRaises(PermissionError):
            add_variant(self.guild, self.player, 'Song', 'Remix')

    def test_removing_a_variant(self):
        add_variant(self.guild, self.host, 'Song', 'Remix')
        remove_variant(self.guild, self.host, 'Song', 'remix')
        self.assertEqual(variants_of(self.guild, self.host, 'Song'), [])

    def test_removing_a_missing_variant_is_refused(self):
        with self.assertRaises(ValueError):
            remove_variant(self.guild, self.host, 'Song', 'Nope')

    def test_a_question_registers_the_variants_of_both_answers(self):
        add_question(self.guild, self.host, 'Encore',
                              secondary_text='Live Band',
                              expected_variants=['Encore (Live)'],
                              secondary_variants=['Live Band (Remix)'])
        self.assertEqual(variants_of(self.guild, self.host, 'Encore'),
                         ['Encore (Live)'])
        self.assertEqual(variants_of(self.guild, self.host, 'Live Band'),
                         ['Live Band (Remix)'])


class CacheTests(GameTestCase):
    """A derived list is read once, and a write forgets what it changes."""

    def test_the_hosts_of_a_guild_are_read_once(self):
        hosts_of(self.guild)
        with self.assertNumQueries(0):
            self.assertIn('<@42>', hosts_of(self.guild))

    def test_a_new_host_is_seen_at_once(self):
        hosts_of(self.guild)
        with self.captureOnCommitCallbacks(execute=True):
            add_host(self.guild, user_mention(50), self.admin)
        self.assertIn('<@50>', hosts_of(self.guild))

    def test_a_removed_host_disappears_at_once(self):
        hosts_of(self.guild)
        with self.captureOnCommitCallbacks(execute=True):
            remove_host(self.guild, user_mention(42), self.admin)
        self.assertNotIn('<@42>', hosts_of(self.guild))

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
        question_choices(game, self.host_player)
        with self.assertNumQueries(0):
            self.assertTrue(question_choices(game, self.host_player))

    def test_the_question_options_search_the_labels(self):
        game = self.create_game()
        found = question_choices(game, self.host_player, 'other')
        self.assertEqual([choice['label'] for choice in found], ['Other'])

    def test_a_new_question_is_offered_at_once(self):
        game = self.create_game()
        question_choices(game, self.host_player)
        with self.captureOnCommitCallbacks(execute=True):
            add_question(self.guild, self.host, 'Fresh Song')
        labels = [choice['label']
                  for choice in question_choices(game, self.host_player)]
        self.assertTrue(any('Fresh Song' in label for label in labels))

    def test_an_edited_answer_is_seen_at_once(self):
        game = self.create_game()
        own = Question.objects.get(
            pk=add_question(self.guild, self.host, 'Song')['pk'])
        question_choices(game, self.host_player)
        with self.captureOnCommitCallbacks(execute=True):
            edit_question(self.guild, self.host, own.pk,
                                   answer='Renamed')
        labels = [choice['label']
                  for choice in question_choices(game, self.host_player)]
        self.assertTrue(any('Renamed' in label for label in labels))

    def test_a_played_question_leaves_the_picker(self):
        game = self.create_game()
        played = question_choices(game, self.host_player)[0]['pk']
        with self.captureOnCommitCallbacks(execute=True):
            open_round(game, self.host,
                                 question_by_pk(played))
        left = [choice['pk']
                for choice in question_choices(game, self.host_player)]
        self.assertNotIn(played, left)

    def test_the_library_options_are_read_once(self):
        library_choices(self.guild, self.host_player)
        with self.assertNumQueries(0):
            self.assertEqual(library_choices(self.guild, self.host_player), [])

    def test_the_queued_options_are_read_once(self):
        game = self.create_game()
        create_round(game, self.host, self.question, index=1)
        queued_choices(game)
        with self.assertNumQueries(0):
            self.assertEqual(len(queued_choices(game)), 1)

    def test_the_game_options_are_read_once(self):
        self.create_game()
        game_choices(self.guild)
        with self.assertNumQueries(0):
            self.assertEqual(len(game_choices(self.guild)), 1)

    def test_the_queue_of_a_game_is_not_read_for_another_game(self):
        # A finished game clears the one-active-game constraint of the guild.
        other = create_game(self.guild, 100, self.host,
                                     state=Game.State.FINISHED)
        queued = self.create_game()
        create_round(queued, self.host, self.question, index=1)
        queued_choices(queued)
        self.assertEqual(queued_choices(other), [])
        offered = [choice['pk']
                   for choice in question_choices(other, self.host_player)]
        self.assertIn(self.question.pk, offered)

    def test_a_library_too_large_to_cache_falls_back_to_the_database(self):
        game = self.create_game()
        with mock.patch.object(caching, 'LIBRARY_CACHE_LIMIT', 1):
            pks = [choice['pk']
                   for choice in question_choices(game, self.host_player)]
        self.assertIn(self.question.pk, pks)
        self.assertIn(self.other_question.pk, pks)


class ScoreboardQueryTests(GameTestCase):
    """A scoreboard reads the rounds of a game once, whatever their number."""

    def _play(self, rounds: int) -> Game:
        """Return a game with as many played, guessed and revealed rounds."""
        game = self.create_game()
        for _count in range(rounds):
            round_ = Round.objects.get(
                pk=open_round(game, self.host, self.question)['round_id'])
            submit_guess(round_, self.player_row, 'Song')
            reveal_round(round_, self.host)
        return game

    def test_the_player_scores_do_not_grow_with_the_rounds(self):
        def queries(rounds: int) -> int:
            game = self._play(rounds)
            with CaptureQueriesContext(connection) as captured:
                game_scores(game)
            end_game(game, self.host)
            return len(captured)
        self.assertEqual(queries(1), queries(3))

    def test_the_recap_does_not_grow_with_the_rounds(self):
        def queries(rounds: int) -> int:
            game = self._play(rounds)
            with CaptureQueriesContext(connection) as captured:
                end_game(game, self.host)
            return len(captured)
        self.assertEqual(queries(1), queries(3))


class BroadcastTests(GameTestCase):
    """The public posts a game owes, and the client that makes them."""

    def prepared_game(self) -> Game:
        """Return a game waiting to be published."""
        return create_game(self.guild, 100, self.host,
                                   state=Game.State.SETUP)

    def running_game(self) -> Game:
        """Return a published game with no round open."""
        return self.create_game()

    def played_game(self) -> Game:
        """Return a game with a round open in it."""
        game = self.create_game()
        open_round(game, self.host)
        return game

    def test_opening_a_round_records_the_post_it_owes(self):
        payload, broadcast = post_round_open(self.running_game(), self.host)
        self.assertEqual(broadcast.kind, Broadcast.Kind.ROUND)
        self.assertEqual(broadcast.status, Broadcast.Status.PENDING)
        self.assertEqual(broadcast.round_id, payload['round_id'])
        self.assertIsNone(broadcast.sent_at)

    def test_publishing_records_the_publication_it_owes(self):
        _payload, broadcast = post_publication(self.prepared_game(),
                                                     self.host)
        self.assertEqual(broadcast.kind, Broadcast.Kind.PUBLISH)
        self.assertIsNone(broadcast.round_id)

    def test_a_refused_transition_records_no_post(self):
        with self.assertRaises(ValueError):
            post_publication(self.create_game(), self.host)
        self.assertEqual(Broadcast.objects.count(), 0)

    def test_a_claimed_post_is_left_to_the_client_that_owns_it(self):
        _payload, broadcast = post_round_open(
            self.running_game(), self.host, claim=True)
        self.assertEqual(broadcast.status, Broadcast.Status.CLAIMED)
        self.assertFalse(claim_broadcast(broadcast.pk))
        self.assertEqual(pending_broadcasts(), [])

    def test_only_one_client_takes_a_post(self):
        _payload, broadcast = post_round_open(self.running_game(),
                                                  self.host)
        self.assertTrue(claim_broadcast(broadcast.pk))
        self.assertFalse(claim_broadcast(broadcast.pk))

    def _expire(self, broadcast: Broadcast) -> None:
        """Make a held post look abandoned by the client that owns it."""
        Broadcast.objects.filter(pk=broadcast.pk).update(
            claimed_at=timezone.now()
            - timedelta(seconds=BROADCAST_CLAIM_TIMEOUT + 1))

    def test_a_post_held_for_too_long_is_taken_over(self):
        _payload, broadcast = post_round_open(
            self.running_game(), self.host, claim=True)
        self.assertEqual(pending_broadcasts(), [])
        self._expire(broadcast)
        self.assertEqual([b.pk for b in pending_broadcasts()],
                         [broadcast.pk])
        self.assertTrue(claim_broadcast(broadcast.pk))

    def test_the_posts_come_oldest_first(self):
        game = self.running_game()
        first = enqueue(game, Broadcast.Kind.PUBLISH)
        second = enqueue(game, Broadcast.Kind.RECAP)
        self.assertEqual([b.pk for b in pending_broadcasts()],
                         [first.pk, second.pk])

    def test_a_finished_game_owes_the_answer_and_the_scores(self):
        _payload, posts = post_game_end(self.played_game(), self.host)
        self.assertEqual([broadcast.kind for broadcast, _payload in posts],
                         [Broadcast.Kind.REVEAL, Broadcast.Kind.RECAP])

    def test_a_game_ended_with_no_open_round_owes_only_the_scores(self):
        _payload, posts = post_game_end(self.create_game(), self.host)
        self.assertEqual([broadcast.kind for broadcast, _payload in posts],
                         [Broadcast.Kind.RECAP])

    def test_a_post_read_later_shows_what_it_showed(self):
        payload, broadcast = post_round_open(self.running_game(), self.host)
        self.assertEqual(broadcast_payload(broadcast), payload)

    def test_the_answer_and_the_scores_read_the_same_later(self):
        payload, posts = post_game_end(self.played_game(), self.host)
        reveal, recap = posts
        self.assertEqual(broadcast_payload(reveal[0]), payload['reveal'])
        self.assertEqual(
            broadcast_payload(recap[0]),
            {key: value for key, value in payload.items() if key != 'reveal'})

    def test_a_post_keeps_the_messages_it_produced(self):
        _payload, broadcast = post_round_open(self.running_game(),
                                                  self.host)
        mark_broadcast_sent(broadcast, [11, 12])
        self.assertEqual(broadcast.status, Broadcast.Status.SENT)
        self.assertEqual(broadcast.message_ids, [11, 12])
        self.assertIsNotNone(broadcast.sent_at)
        self.assertEqual(pending_broadcasts(), [])

    def test_a_post_that_could_not_be_made_keeps_its_reason(self):
        _payload, broadcast = post_round_open(self.running_game(),
                                                  self.host)
        mark_broadcast_failed(broadcast, 'its channel is gone')
        self.assertEqual(broadcast.status, Broadcast.Status.FAILED)
        self.assertEqual(broadcast.error, 'its channel is gone')
        # A failed post is not retried behind the caller's back.
        self.assertEqual(pending_broadcasts(), [])
