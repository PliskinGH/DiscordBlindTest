"""Tests for the Discord data models: players, guilds, hosts and mentions."""

from collections.abc import Iterable

from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import SimpleTestCase, TestCase

from . import members, mentions
from .models import Guild, Host, Player


class FakeRole:
    """Minimal stand-in for a ``discord.Role``."""

    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakePermissions:
    """Minimal stand-in for ``discord.Permissions``."""

    def __init__(self, manage_guild: bool = False) -> None:
        self.manage_guild = manage_guild


class FakeDiscordUser:
    """Minimal stand-in for a ``discord.User``/``discord.Member``."""

    def __init__(self, user_id: int, name: str,
                 roles: Iterable[int] = (),
                 manage_guild: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.roles = [FakeRole(role_id) for role_id in roles]
        self.guild_permissions = FakePermissions(manage_guild)


class FakeDiscordGuild:
    """Minimal stand-in for a ``discord.Guild``."""

    def __init__(self, guild_id: int, name: str) -> None:
        self.id = guild_id
        self.name = name


class PlayerTests(TestCase):
    def test_from_discord_creates_the_player(self):
        player = Player.objects.from_discord(FakeDiscordUser(42, 'alice'))
        self.assertEqual(player.discord_user_id, 42)
        self.assertEqual(player.discord_name, 'alice')
        self.assertEqual(player.username, 'alice')

    def test_from_discord_is_idempotent(self):
        first = Player.objects.from_discord(FakeDiscordUser(42, 'alice'))
        second = Player.objects.from_discord(FakeDiscordUser(42, 'alice'))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Player.objects.count(), 1)

    def test_from_discord_follows_a_rename(self):
        Player.objects.from_discord(FakeDiscordUser(42, 'alice'))
        player = Player.objects.from_discord(FakeDiscordUser(42, 'alice2'))
        self.assertEqual(player.discord_name, 'alice2')
        self.assertEqual(player.username, 'alice2')

    def test_a_manually_renamed_player_keeps_their_username(self):
        player = Player.objects.from_discord(FakeDiscordUser(42, 'alice'))
        player.username = 'custom_name'
        player.save(update_fields=['username'])
        player = Player.objects.from_discord(FakeDiscordUser(42, 'alice2'))
        self.assertEqual(player.discord_name, 'alice2')
        self.assertEqual(player.username, 'custom_name')

    def test_a_rename_does_not_take_another_players_username(self):
        Player.objects.from_discord(FakeDiscordUser(42, 'alice'))
        Player.objects.create_user(username='bob')
        player = Player.objects.from_discord(FakeDiscordUser(42, 'bob'))
        self.assertEqual(player.discord_name, 'bob')
        self.assertEqual(player.username, 'alice')

    def test_a_username_taken_locally_falls_back_to_the_id(self):
        Player.objects.create_user(username='alice')
        player = Player.objects.from_discord(FakeDiscordUser(42, 'alice'))
        self.assertEqual(player.username, 'discord_42')
        self.assertEqual(player.discord_name, 'alice')

    def test_a_later_claim_supersedes_a_stale_holder(self):
        first = Player.objects.from_discord(FakeDiscordUser(1, 'taken'))
        second = Player.objects.from_discord(FakeDiscordUser(2, 'taken'))
        first.refresh_from_db()
        self.assertEqual(first.username, 'discord_1')
        self.assertIsNone(first.discord_name)
        self.assertEqual(second.username, 'taken')
        self.assertEqual(second.discord_name, 'taken')

    def test_a_rename_displaces_the_stale_holder(self):
        player = Player.objects.from_discord(FakeDiscordUser(1, 'alice'))
        other = Player.objects.create_user(username='other', discord_name='bob')
        player = Player.objects.from_discord(FakeDiscordUser(1, 'bob'))
        other.refresh_from_db()
        self.assertEqual(other.username, 'other')
        self.assertIsNone(other.discord_name)
        self.assertEqual(player.username, 'bob')
        self.assertEqual(player.discord_name, 'bob')

    def test_a_name_swap_updates_both_players(self):
        Player.objects.from_discord(FakeDiscordUser(1, 'alice'))
        Player.objects.from_discord(FakeDiscordUser(2, 'bob'))
        Player.objects.from_discord(FakeDiscordUser(1, 'bob'))
        second = Player.objects.from_discord(FakeDiscordUser(2, 'alice'))
        first = Player.objects.get(discord_user_id=1)
        self.assertEqual((first.username, first.discord_name), ('bob', 'bob'))
        self.assertEqual((second.username, second.discord_name),
                         ('alice', 'alice'))

    def test_a_name_swap_updates_both_players_in_reverse_order(self):
        Player.objects.from_discord(FakeDiscordUser(1, 'alice'))
        Player.objects.from_discord(FakeDiscordUser(2, 'bob'))
        second = Player.objects.from_discord(FakeDiscordUser(2, 'alice'))
        Player.objects.from_discord(FakeDiscordUser(1, 'bob'))
        first = Player.objects.get(discord_user_id=1)
        self.assertEqual((first.username, first.discord_name), ('bob', 'bob'))
        self.assertEqual((second.username, second.discord_name),
                         ('alice', 'alice'))


class GuildTests(TestCase):
    def test_from_discord_creates_the_guild(self):
        guild = Guild.objects.from_discord(FakeDiscordGuild(1, 'Server'))
        self.assertEqual(guild.discord_id, 1)
        self.assertEqual(guild.name, 'Server')

    def test_from_discord_is_idempotent(self):
        first = Guild.objects.from_discord(FakeDiscordGuild(1, 'Server'))
        second = Guild.objects.from_discord(FakeDiscordGuild(1, 'Server'))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Guild.objects.count(), 1)

    def test_from_discord_follows_a_rename(self):
        Guild.objects.from_discord(FakeDiscordGuild(1, 'Server'))
        guild = Guild.objects.from_discord(FakeDiscordGuild(1, 'Renamed'))
        self.assertEqual(guild.name, 'Renamed')


class HostTests(TestCase):
    def setUp(self):
        self.guild = Guild.objects.create(discord_id=1, name='Server')

    def test_a_user_mention_is_normalised_on_save(self):
        host = Host.objects.create(guild=self.guild, mention='<@!42>')
        self.assertEqual(host.mention, '<@42>')
        self.assertFalse(host.is_role)
        self.assertEqual(host.discord_id, 42)

    def test_a_role_mention_is_recognised(self):
        host = Host.objects.create(guild=self.guild, mention='<@&99>')
        self.assertTrue(host.is_role)
        self.assertEqual(host.discord_id, 99)

    def test_a_guild_cannot_list_the_same_host_twice(self):
        Host.objects.create(guild=self.guild, mention='<@42>')
        with self.assertRaises(IntegrityError):
            Host.objects.create(guild=self.guild, mention='<@!42>')

    def test_a_malformed_mention_is_rejected(self):
        host = Host(guild=self.guild, mention='alice')
        with self.assertRaises(ValidationError):
            host.full_clean()

    def test_the_same_host_may_be_listed_in_two_guilds(self):
        other = Guild.objects.create(discord_id=2, name='Other')
        Host.objects.create(guild=self.guild, mention='<@42>')
        Host.objects.create(guild=other, mention='<@42>')
        self.assertEqual(Host.objects.count(), 2)


class MentionsTests(SimpleTestCase):
    def test_parse_mention_reads_users_and_roles(self):
        self.assertEqual(mentions.parse_mention('<@123>'), (False, 123))
        self.assertEqual(mentions.parse_mention('<@!123>'), (False, 123))
        self.assertEqual(mentions.parse_mention('<@&123>'), (True, 123))

    def test_parse_mention_rejects_anything_else(self):
        for value in ('123', 'alice', '<@>', '<@abc>', ''):
            self.assertIsNone(mentions.parse_mention(value), value)

    def test_normalize_mention_rewrites_the_legacy_user_prefix(self):
        self.assertEqual(mentions.normalize_mention(' <@!42> '), '<@42>')


class MemberTests(SimpleTestCase):
    def test_member_mentions_include_the_users_roles(self):
        member = FakeDiscordUser(42, 'alice', roles=[7, 8])
        self.assertEqual(members.member_mentions(member),
                         {'<@42>', '<@&7>', '<@&8>'})

    def test_member_mentions_without_roles(self):
        member = FakeDiscordUser(42, 'alice')
        self.assertEqual(members.member_mentions(member), {'<@42>'})

    def test_can_manage_guild_reads_the_discord_permission(self):
        self.assertFalse(members.can_manage_guild(FakeDiscordUser(1, 'alice')))
        self.assertTrue(members.can_manage_guild(
            FakeDiscordUser(1, 'alice', manage_guild=True)))
