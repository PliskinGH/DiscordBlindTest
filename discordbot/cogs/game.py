"""Slash commands and the operations behind the controls of a game."""

import logging
from collections import OrderedDict

import discord
from discord import app_commands
from discord.ext import commands, tasks

from blindtest.models import Broadcast, Game, Question, QuizType, ScoringMode
from discordcore.models import Guild
from blindtest.services.broadcasts import (broadcast_payload,
                                           claim_broadcast,
                                           mark_broadcast_failed,
                                           mark_broadcast_sent,
                                           pending_broadcasts,
                                           post_game_end, post_publication,
                                           post_round_open, post_round_reveal)
from blindtest.services.games import (active_game, clear_queue,
                                      copy_questions_by_pk, create_game,
                                      game_choices, game_summary, panel_data,
                                      queue_questions, queued_choices,
                                      unqueue_questions)
from blindtest.services.guessing import (guess_form, round_display,
                                         submit_guess,
                                         submit_multiple_choice)
from blindtest.services.guilds import (games_count, is_host, require_host,
                                       target_channel_id, target_ping_role_id)
from blindtest.services.library import question_by_pk, question_choices
from blindtest.services.rounds import create_round, current_round

from .. import embeds
from ..db import guild_for, player_for, run_db
from ..ui import (ANSWER_TITLE, GamePanel, GuessFormPanel, GuessModal,
                  HostPanel, QueuePanel, SetupPanel, disabled, option_label,
                  picked_pk)

logger = logging.getLogger(__name__)

# Guess forms primed by the rounds opened in this process.
MAX_CACHED_FORMS = 20
# How often the client posts the broadcasts recorded without one.
BROADCAST_POLL_SECONDS = 1.0

# The channels Discord offers for a game: any channel or thread of the server.
GameChannel = discord.abc.GuildChannel | discord.Thread

# Said wherever a command needs the game of the server and finds none.
NO_QUIZ_RUNNING = 'No game is running in this server.'

# Descriptions both groups share; only setup, queue and next differ.
PUBLISH_DESCRIPTION = 'Publish the game being prepared.'
PANEL_DESCRIPTION = 'Reopen the private controls of the running game.'
GUESS_DESCRIPTION = 'Submit your guess for the round in play.'
END_DESCRIPTION = 'End the game of this server.'
REVEAL_DESCRIPTION = 'Reveal the current round.'
UNQUEUE_DESCRIPTION = 'Remove a question queued for a round.'
CLEAR_DESCRIPTION = 'Remove every queued question of the game.'
COPY_DESCRIPTION = 'Queue the questions another game was played with.'

# Options the two commands of a subcommand share. The ones that name a quiz
# type add it on top, since only the quiz group offers it.
SETUP_OPTIONS = {'scoring': 'How the points are awarded.',
                 'name': 'Name of the game, e.g. Blind Test 2026.',
                 'channel': 'Channel to play in.',
                 'role': 'Role to ping.'}
GUESS_OPTIONS = {'answer': 'Your answer, e.g. the music title.',
                 'secondary_answer': 'Artist for blind tests, or '
                                     'secondary required answer.'}
QUEUE_OPTIONS = {'question': 'Question to play next.'}
UNQUEUE_OPTIONS = {'question': 'Queued question to remove.'}
COPY_OPTIONS = {'source': 'Game to copy the questions from.'}


def named_channel_id(interaction: discord.Interaction,
                     channel: GameChannel | None) -> int | None:
    """Return the ID of the channel the host named, or None when it named none.

    The order of preference between the channels of a game is the domain's, in
    ``target_channel_id``; this only checks that the named channel is
    one of this server's.
    """
    if channel is None:
        return None
    if channel.guild.id != interaction.guild.id:
        raise ValueError("That channel belongs to another server.")
    return channel.id


def require_pingable(interaction: discord.Interaction,
                     role: discord.Role) -> None:
    """Raise ValueError when the bot cannot ping this role.

    A role above the bot's highest one makes Discord reject every message
    naming it, which would swallow the publication and every round.
    """
    if role.guild.id != interaction.guild.id:
        raise ValueError("That role belongs to another server.")
    if role.position >= interaction.guild.me.top_role.position:
        raise ValueError("That role is above me: I cannot ping it.")


def named_ping_role_id(interaction: discord.Interaction,
                       role: discord.Role | None) -> int | None:
    """Return the ID of the role the host named, or None when it named none.

    Which role a game pings is the domain's prerogative, in
    ``target_ping_role_id``; this only checks that the bot may use it.
    """
    if role is None:
        return None
    require_pingable(interaction, role)
    return role.id


def ping_text(payload: dict) -> str:
    """Return the role ping formatted for discord."""
    role = payload.get('ping_role_id')
    return f'<@&{role}>' if role else ''


def summary_line(data: dict) -> str:
    """Return the line naming a game and the settings it was set up with."""
    parts = [f'**{data["game_name"]}**', data['type_label'],
             f'<#{data["channel_id"]}>']
    if data.get('ping_role_id'):
        parts.append(f'pings <@&{data["ping_role_id"]}>')
    return ' · '.join(parts)


def setup_text(data: dict, lead: str = '') -> str:
    """Return the lines naming the game being set up and its queue."""
    return '\n'.join(filter(None, [
        lead, summary_line(data),
        '{} question(s) queued. Add, remove or copy questions, then '
        'publish.'.format(data['queued'])]))


def host_text(data: dict, lead: str = '') -> str:
    """Return the lines naming the running game and the controls it offers."""
    return '\n'.join(filter(None, [
        lead, summary_line(data),
        'Next round, reveal, queue or end.']))


def added_text(result: dict) -> str:
    """Return how a queueing operation reports what it queued."""
    return '{} question(s) added, {} skipped.'.format(result['added'],
                                                      result['skipped'])


def removed_text(removed: int) -> str:
    """Return how a removal reports what it removed."""
    return '{} question(s) removed.'.format(removed)


def unqueued_text(removed: int) -> str:
    """Return how removing one queued question reports itself."""
    return removed_text(removed) if removed else 'That question is not queued.'


def chosen_value(raw: str, choices) -> str:
    """Return the value of a static choice, by value or by display label."""
    wanted = str(raw).strip()
    for choice in choices:
        if wanted in (choice.value, str(choice.label)):
            return choice.value
    return wanted


def guess_form_title(interaction: discord.Interaction) -> str:
    """Return the title of a guess form: the bot's name in the server."""
    guild = interaction.guild
    return guild.me.display_name if guild is not None else ANSWER_TITLE


def posted_line(text: str, messages: 'Iterable[discord.Message]') -> str:
    """Return a report of what was posted, linking every post it named."""
    links = ' '.join(message.jump_url for message in messages
                     if getattr(message, 'jump_url', None))
    return f'{text.rstrip(".")}: {links}.' if links else text


def round_note(text: str, result: dict) -> str:
    """Return a host note of a round with its listening link, if any."""
    media = embeds.media_line(result['media_url'])
    return f'{text}\n{media}' if media else text


async def question_autocomplete(
        interaction: discord.Interaction,
        current: str) -> list[app_commands.Choice[str]]:
    """Offer the questions of this server's library the game did not use yet."""
    try:
        guild = await guild_for(interaction)
        game = await run_db(active_game, guild)
        if game is None:
            return []
        questions = await run_db(question_choices, game, current)
        return [app_commands.Choice(name=option_label(choice),
                                    value=str(choice['pk']))
                for choice in questions]
    except Exception:
        logger.exception('Failed to offer question choices')
        return []


async def queued_autocomplete(
        interaction: discord.Interaction,
        current: str) -> list[app_commands.Choice[str]]:
    """Offer the questions queued for the game of this server."""
    try:
        guild = await guild_for(interaction)
        game = await run_db(active_game, guild)
        if game is None:
            return []
        queued = await run_db(queued_choices, game, current)
        return [app_commands.Choice(name=option_label(choice),
                                    value=str(choice['pk']))
                for choice in queued]
    except Exception:
        logger.exception('Failed to offer queued choices')
        return []


async def game_autocomplete(
        interaction: discord.Interaction,
        current: str) -> list[app_commands.Choice[str]]:
    """Offer the games of this server whose questions can be copied."""
    try:
        guild = await guild_for(interaction)
        games = await run_db(game_choices, guild, None, current)
        return [app_commands.Choice(name=option_label(choice),
                                    value=str(choice['pk']))
                for choice in games]
    except Exception:
        logger.exception('Failed to offer game choices')
        return []


class GameCog(commands.Cog):
    """Slash commands and the operations behind the controls of a game."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.forms: OrderedDict[int, dict] = OrderedDict()

    async def cog_load(self) -> None:
        """Revive the game controls, so a bot restart does not break a game."""
        for view in self.controls():
            self.bot.add_view(view)
            logger.info('Registered persistent view %s', type(view).__name__)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start posting the recorded broadcasts once the client is connected."""
        if not self.broadcast_loop.is_running():
            self.broadcast_loop.start()

    def controls(self) -> list[discord.ui.View]:
        """Return the controls a restart has to revive, as they are stateless."""
        return [GamePanel(self), HostPanel(self), QueuePanel(self), SetupPanel(self)]

    async def defer(self, interaction: discord.Interaction,
                    update_panel: bool = False) -> None:
        """Respond to the interaction before any database work.

        A flow that closes the panel it came from holds its interaction to a
        deferred message update; every other flow defers a private reply.
        """
        panel = interaction.type is discord.InteractionType.component
        if update_panel and panel:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

    # The generic command group, and the legacy name kept as its alias: the same
    # subcommands, except the two naming a quiz type, which fix it to a blind test.
    quiz = app_commands.Group(name='quiz', description='Run a quiz.')
    blindtest = app_commands.Group(name='blindtest',
                                   description='Run a blind test quiz.')

    @app_commands.command(name='ping', description='Check the bot and its database.')
    async def ping_command(self, interaction: discord.Interaction) -> None:
        """Report the gateway latency and the number of games in this server."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            games = await run_db(games_count, guild)
            await interaction.followup.send(
                f'Pong! {self.bot.latency * 1000:.0f} ms - '
                f'{games} game(s) in this server.',
                ephemeral=True)
        except Exception:
            logger.exception('Failed to process ping command')
            await interaction.followup.send('Failed to check database status.', ephemeral=True)

    @quiz.command(name='setup', description='Set up a game in a channel.')
    @app_commands.describe(**SETUP_OPTIONS,
                           quiz_type='How the rounds are played.')
    @app_commands.choices(scoring=[
        app_commands.Choice(name=str(mode.label), value=mode.value)
        for mode in ScoringMode], quiz_type=[
        app_commands.Choice(name=str(kind.label), value=kind.value)
        for kind in QuizType])
    async def quiz_setup_command(self, interaction: discord.Interaction,
                                 scoring: str = ScoringMode.STANDARD,
                                 name: str = '',
                                 channel: GameChannel | None = None,
                                 role: discord.Role | None = None,
                                 quiz_type: str = QuizType.BLIND_TEST) -> None:
        """Open a game to be set up, hosted by the player invoking the command."""
        await self.setup_game(interaction, scoring, name, quiz_type, channel, role)

    @blindtest.command(name='setup',
                       description='Set up a blind test game in a channel.')
    @app_commands.describe(**SETUP_OPTIONS)
    @app_commands.choices(scoring=[
        app_commands.Choice(name=str(mode.label), value=mode.value)
        for mode in ScoringMode])
    async def blindtest_setup_command(self, interaction: discord.Interaction,
                                      scoring: str = ScoringMode.STANDARD,
                                      name: str = '',
                                      channel: GameChannel | None = None,
                                      role: discord.Role | None = None) -> None:
        """Open a game to be set up, always played as a blind test."""
        await self.setup_game(interaction, scoring, name, QuizType.BLIND_TEST,
                              channel, role)

    @quiz.command(name='publish', description=PUBLISH_DESCRIPTION)
    async def quiz_publish_command(self, interaction: discord.Interaction) -> None:
        """Publish the game the host prepared."""
        await self.publish(interaction)

    @blindtest.command(name='publish', description=PUBLISH_DESCRIPTION)
    async def blindtest_publish_command(self,
                                        interaction: discord.Interaction) -> None:
        """Publish the game the host prepared."""
        await self.publish(interaction)

    @quiz.command(name='panel', description=PANEL_DESCRIPTION)
    async def quiz_panel_command(self, interaction: discord.Interaction) -> None:
        """Send the host panel of the running game again."""
        await self.show_panel(interaction)

    @blindtest.command(name='panel', description=PANEL_DESCRIPTION)
    async def blindtest_panel_command(self, interaction: discord.Interaction) -> None:
        """Send the host panel of the running game again."""
        await self.show_panel(interaction)

    @quiz.command(name='guess', description=GUESS_DESCRIPTION)
    @app_commands.describe(**GUESS_OPTIONS)
    async def quiz_guess_command(self, interaction: discord.Interaction,
                                 answer: str, secondary_answer: str = '') -> None:
        """Record the guess of the player invoking the command."""
        await self.record_guess(interaction, answer, secondary_answer)

    @blindtest.command(name='guess', description=GUESS_DESCRIPTION)
    @app_commands.describe(**GUESS_OPTIONS)
    async def blindtest_guess_command(self, interaction: discord.Interaction,
                                      answer: str,
                                      secondary_answer: str = '') -> None:
        """Record the guess of the player invoking the command."""
        await self.record_guess(interaction, answer, secondary_answer)

    @quiz.command(name='end', description=END_DESCRIPTION)
    async def quiz_end_command(self, interaction: discord.Interaction) -> None:
        """Close the running game and publish the final scores."""
        await self.end_game(interaction)

    @blindtest.command(name='end', description=END_DESCRIPTION)
    async def blindtest_end_command(self, interaction: discord.Interaction) -> None:
        """Close the running game and publish the final scores."""
        await self.end_game(interaction)

    @quiz.command(name='next', description='Open the next round of the game.')
    async def quiz_next_command(self, interaction: discord.Interaction) -> None:
        """Open the next round, starting a queued one or drawing a question."""
        await self.open_next_round(interaction)

    @blindtest.command(name='next',
                       description='Open the next round of the blind test.')
    async def blindtest_next_command(self,
                                     interaction: discord.Interaction) -> None:
        """Open the next round, starting a queued one or drawing a question."""
        await self.open_next_round(interaction)

    @quiz.command(name='reveal', description=REVEAL_DESCRIPTION)
    async def quiz_reveal_command(self, interaction: discord.Interaction) -> None:
        """Close the round and publish its answer with the standings."""
        await self.reveal_round(interaction)

    @blindtest.command(name='reveal', description=REVEAL_DESCRIPTION)
    async def blindtest_reveal_command(self,
                                       interaction: discord.Interaction) -> None:
        """Close the round and publish its answer with the standings."""
        await self.reveal_round(interaction)

    @quiz.command(name='queue', description='Queue a question for the next round.')
    @app_commands.describe(
        **QUEUE_OPTIONS,
        quiz_type='BLIND_TEST, OPEN or MULTIPLE_CHOICE; defaults to the game type.')
    @app_commands.autocomplete(question=question_autocomplete)
    async def quiz_queue_command(self, interaction: discord.Interaction,
                                 question: str, quiz_type: str = '') -> None:
        """Pre-select the question of the next round."""
        await self.queue_question(interaction, question, quiz_type)

    @blindtest.command(name='queue',
                       description='Queue a blind test question for the next round.')
    @app_commands.describe(**QUEUE_OPTIONS)
    @app_commands.autocomplete(question=question_autocomplete)
    async def blindtest_queue_command(self, interaction: discord.Interaction,
                                      question: str) -> None:
        """Pre-select the question of the next round, as a blind test."""
        await self.queue_question(interaction, question, QuizType.BLIND_TEST)

    @quiz.command(name='unqueue', description=UNQUEUE_DESCRIPTION)
    @app_commands.describe(**UNQUEUE_OPTIONS)
    @app_commands.autocomplete(question=queued_autocomplete)
    async def quiz_unqueue_command(self, interaction: discord.Interaction,
                                   question: str) -> None:
        """Remove a queued question of the game."""
        await self.unqueue_question(interaction, question)

    @blindtest.command(name='unqueue', description=UNQUEUE_DESCRIPTION)
    @app_commands.describe(**UNQUEUE_OPTIONS)
    @app_commands.autocomplete(question=queued_autocomplete)
    async def blindtest_unqueue_command(self, interaction: discord.Interaction,
                                        question: str) -> None:
        """Remove a queued question of the game."""
        await self.unqueue_question(interaction, question)

    @quiz.command(name='clear', description=CLEAR_DESCRIPTION)
    async def quiz_clear_command(self, interaction: discord.Interaction) -> None:
        """Empty the queue of the game."""
        await self.clear(interaction)

    @blindtest.command(name='clear', description=CLEAR_DESCRIPTION)
    async def blindtest_clear_command(self, interaction: discord.Interaction) -> None:
        """Empty the queue of the game."""
        await self.clear(interaction)

    @quiz.command(name='copy', description=COPY_DESCRIPTION)
    @app_commands.describe(**COPY_OPTIONS)
    @app_commands.autocomplete(source=game_autocomplete)
    async def quiz_copy_command(self, interaction: discord.Interaction,
                                source: str) -> None:
        """Queue the questions of another game."""
        await self.copy_questions(interaction, source)

    @blindtest.command(name='copy', description=COPY_DESCRIPTION)
    @app_commands.describe(**COPY_OPTIONS)
    @app_commands.autocomplete(source=game_autocomplete)
    async def blindtest_copy_command(self, interaction: discord.Interaction,
                                     source: str) -> None:
        """Queue the questions of another game."""
        await self.copy_questions(interaction, source)


    async def refuse_setup(self, interaction: discord.Interaction,
                           guild: Guild, error: Exception) -> None:
        """Refuse a setup, beside the controls of the game already there.

        The option checks run before the host check, so only a host may see the
        panels.
        """
        host = await run_db(is_host, guild, interaction.user)
        game = await run_db(active_game, guild) if host else None
        if game is None:
            await interaction.followup.send(str(error), ephemeral=True)
        elif game.is_preparing:
            await self.send_setup_panel(
                interaction, await run_db(panel_data, game),
                str(error))
        else:
            await self.send_panel(
                interaction,
                host_text(await run_db(game_summary, game), str(error)))

    async def setup_game(self, interaction: discord.Interaction, scoring: str,
                         name: str = '', quiz_type: str = '',
                         channel: GameChannel | None = None,
                         role: discord.Role | None = None) -> bool:
        """Set up a new game and hand its setup controls to the host."""
        await self.defer(interaction)
        scoring = chosen_value(scoring, ScoringMode)
        quiz_type = chosen_value(quiz_type, QuizType) if quiz_type else quiz_type
        try:
            guild = await guild_for(interaction)
            channel_id = named_channel_id(interaction, channel)
            ping_role_id = named_ping_role_id(interaction, role)
            game = await run_db(create_game, guild, channel_id,
                                interaction.user, scoring, name=name,
                                quiz_type=quiz_type or QuizType.BLIND_TEST,
                                state=Game.State.SETUP,
                                invoking_id=interaction.channel_id,
                                ping_role_id=ping_role_id)
            data = await run_db(panel_data, game)
            await self.send_setup_panel(interaction, data)
        except PermissionError:
            await interaction.followup.send(
                'Only hosts of this server can set up a game.',
                ephemeral=True)
            return False
        except ValueError as error:
            await self.refuse_setup(interaction, guild, error)
            return False
        except Exception:
            logger.exception('Failed to set up the game')
            await interaction.followup.send(
                'Could not set up the game (one may already be running '
                'in this server).', ephemeral=True)
            return False
        return True

    async def send_panel(self, interaction: discord.Interaction, text: str) -> None:
        """Send the caller their private host controls."""
        await interaction.followup.send(text, ephemeral=True, view=HostPanel(self))

    def setup_view(self, data: dict) -> SetupPanel:
        """Return the setup controls built from the panel data of a game."""
        return SetupPanel(self, data['choices'], data['queued_choices'],
                          data['games'])

    async def send_setup_panel(self, interaction: discord.Interaction,
                               data: dict, lead: str = '') -> None:
        """Send the caller the controls of the game being prepared."""
        await interaction.followup.send(setup_text(data, lead), ephemeral=True,
                                        view=self.setup_view(data))

    async def show_panel(self, interaction: discord.Interaction) -> bool:
        """Send the controls of the running game to the host asking for them."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            await run_db(require_host, guild, interaction.user)
            game = await run_db(active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            if game.is_preparing:
                await self.send_setup_panel(
                    interaction, await run_db(panel_data, game))
            else:
                await self.send_panel(
                    interaction,
                    host_text(await run_db(game_summary, game)))
        except PermissionError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to open the host panel')
            await interaction.followup.send('Could not open the host panel.',
                                            ephemeral=True)
            return False
        return True


    async def setup_flow(self, interaction: discord.Interaction, operation,
                         text_of, *args, deferred: bool = False) -> bool:
        """Run an operation on the game and refresh the panel it shows.
        ``deferred`` is set when the caller already deferred to read first.
        """
        if not deferred:
            await self.defer(interaction, update_panel=True)
        try:
            guild = await guild_for(interaction)
            game = await run_db(active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            result = await run_db(operation, game, interaction.user, *args)
            data = await run_db(panel_data, game)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to update the game')
            await interaction.followup.send(
                'Could not update the game.', ephemeral=True)
            return False
        if game.is_preparing:
            await self.respond(interaction, text_of(result),
                               panel=self.setup_view(data),
                               content=setup_text(data))
        else:
            await self.respond(interaction, text_of(result))
        return True

    async def queue_selection(self, interaction: discord.Interaction,
                              values: 'list[str]') -> bool:
        """Queue the questions the host picked in the setup panel."""
        return await self.setup_flow(
            interaction, queue_questions, added_text,
            [int(value) for value in values])

    async def remove_selection(self, interaction: discord.Interaction,
                               values: 'list[str]') -> bool:
        """Remove the queued questions the host picked."""
        return await self.setup_flow(
            interaction, unqueue_questions, removed_text,
            [int(value) for value in values])

    async def copy_selection(self, interaction: discord.Interaction,
                             value: str) -> bool:
        """Queue the questions of the game the host picked."""
        return await self.setup_flow(
            interaction, copy_questions_by_pk, added_text, int(value))

    async def clear(self, interaction: discord.Interaction) -> bool:
        """Empty the queue of the game."""
        return await self.setup_flow(interaction, clear_queue,
                                     removed_text)

    async def copy_questions(self, interaction: discord.Interaction,
                             source: str) -> bool:
        """Queue the questions of another game."""
        if source.strip().isdigit():
            return await self.copy_selection(interaction, source)
        await self.defer(interaction)
        guild = await guild_for(interaction)
        game = await run_db(active_game, guild)
        picked = (await picked_pk(
            lambda term: run_db(game_choices, guild, game, term), source)
            if game is not None else None)
        if picked is None:
            await interaction.followup.send('That game no longer exists.',
                                            ephemeral=True)
            return False
        return await self.setup_flow(
            interaction, copy_questions_by_pk, added_text, picked,
            deferred=True)

    async def publish(self, interaction: discord.Interaction) -> bool:
        """Publish the game being prepared."""
        await self.defer(interaction, update_panel=True)
        try:
            guild = await guild_for(interaction)
            game = await run_db(active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            result, broadcast = await run_db(
                post_publication, game, interaction.user, claim=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to publish the game')
            await interaction.followup.send(
                'Could not publish the game.', ephemeral=True)
            return False
        # The game is live, so a publication that fails must not deny it.
        try:
            posted = await self.post_broadcast(
                broadcast, result, fallback=interaction.channel)
        except Exception:
            await interaction.followup.send(
                'The publication could not be posted.', ephemeral=True)
            return True
        text = posted_line('{} is published.'.format(result['game_name']),
                           posted)
        if interaction.type is discord.InteractionType.component:
            await self.respond(interaction, text, panel=HostPanel(self),
                               content=host_text(result))
        else:
            await interaction.followup.send(host_text(result), ephemeral=True,
                                            view=HostPanel(self))
            await interaction.followup.send(text, ephemeral=True)
        return True

    async def open_next_round(self, interaction: discord.Interaction) -> bool:
        """Open the next round and post its Guess button."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            game = await run_db(active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            result, broadcast = await run_db(
                post_round_open, game, interaction.user, claim=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to open the next round')
            await interaction.followup.send(
                'Could not open the next round.', ephemeral=True)
            return False
        try:
            posted = await self.post_broadcast(
                broadcast, result, fallback=interaction.channel)
        except Exception:
            await interaction.followup.send(
                'The round is open, but its message could not be posted.',
                ephemeral=True)
            return False
        note = posted_line('Round {} opened: {}.'.format(
            result['index'], result['host_text']), posted)
        await interaction.followup.send(round_note(note, result), ephemeral=True)
        return True

    async def reveal_round(self, interaction: discord.Interaction) -> bool:
        """Close the round and publish its answer with the standings."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            game = await run_db(active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            round_ = await run_db(current_round, game)
            if round_ is None:
                await interaction.followup.send(
                    'No round is running yet.', ephemeral=True)
                return False
            result, broadcast = await run_db(
                post_round_reveal, round_, interaction.user, claim=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to reveal the round')
            await interaction.followup.send(
                'Could not reveal the round.', ephemeral=True)
            return False
        try:
            posted = await self.post_broadcast(
                broadcast, result, fallback=interaction.channel)
        except Exception:
            await interaction.followup.send(
                'Round {} revealed, but its answer could not be published.'.format(
                    result['index']), ephemeral=True)
            return False
        await interaction.followup.send(
            posted_line('Round {} revealed.'.format(result['index']), posted),
            ephemeral=True)
        return True

    async def post_reveal(self, channel: discord.abc.Messageable,
                          reveal: dict) -> list[discord.Message]:
        """Publish the answer of a revealed round with its guesses and scores."""
        posted = [await embeds.post(channel, embeds=[embeds.reveal_embed(reveal)])]
        if reveal['lines']:
            posted.append(await embeds.post(channel, embeds=[
                embeds.guesses_embed(reveal)]))
        if reveal['scores']:
            posted.append(await embeds.post(channel, embeds=[embeds.scores_embed(
                reveal, f'Round {reveal["index"]} scores',
                embeds.leader_line(reveal['scores'], 'is currently winning'))]))
        return posted

    # The public messages of a game, through one helper: the controls post their
    # own broadcasts as they make them, and the loop posts those a caller
    # without a Discord connection recorded.

    def broadcast_channel(self, game: Game) -> discord.abc.Messageable | None:
        """Return the channel a game is played in, or None when it is gone."""
        channel = self.bot.get_channel(game.channel_id)
        if channel is not None:
            return channel
        guild = self.bot.get_guild(game.guild_id)
        return guild.get_channel(game.channel_id) if guild else None

    async def _deliver(self, broadcast: Broadcast, payload: dict,
                       fallback: discord.abc.Messageable | None = None
                       ) -> list[discord.Message]:
        """Make the public messages one broadcast stands for.

        ``fallback`` is where a flow posts when the channel of the game is gone;
        a client reading the outbox has no such place.
        """
        channel = self.broadcast_channel(broadcast.game) or fallback
        if channel is None:
            raise ValueError('Game {}: its channel is gone.'.format(
                broadcast.game_id))
        if broadcast.kind == Broadcast.Kind.PUBLISH:
            return [await embeds.post(
                channel, content=ping_text(payload),
                embeds=[embeds.publication_embed(
                    payload, payload['host_mention'])])]
        if broadcast.kind == Broadcast.Kind.ROUND:
            message = await embeds.post(channel, content=ping_text(payload),
                                        embeds=[embeds.round_embed(payload)],
                                        view=GamePanel(self))
            self.cache_form(message.id, payload['form'])
            return [message]
        if broadcast.kind == Broadcast.Kind.REVEAL:
            return await self.post_reveal(channel, payload)
        return [await embeds.post(channel, embeds=[embeds.recap_embed(payload)])]

    async def post_broadcast(self, broadcast: Broadcast, payload: dict,
                             fallback: discord.abc.Messageable | None = None,
                             ) -> list[discord.Message]:
        """Post what a broadcast owes, and record the messages it produced.

        The caller owns the row it posts, so this never claims it again.
        """
        try:
            posted = await self._deliver(broadcast, payload, fallback)
        except Exception as error:
            logger.exception('Failed to post broadcast %s', broadcast.pk)
            await run_db(mark_broadcast_failed, broadcast, str(error))
            raise
        await run_db(mark_broadcast_sent, broadcast,
                     [message.id for message in posted])
        return posted

    async def deliver_broadcast(self, broadcast: Broadcast,
                                ) -> list[discord.Message]:
        """Take a broadcast this client owes and post it, unless it was taken."""
        payload = await run_db(broadcast_payload, broadcast)
        if not await run_db(claim_broadcast, broadcast.pk):
            logger.debug('Broadcast %s was already claimed', broadcast.pk)
            return []
        return await self.post_broadcast(broadcast, payload)

    @tasks.loop(seconds=BROADCAST_POLL_SECONDS)
    async def broadcast_loop(self) -> None:
        """Post the broadcasts a caller without a client recorded."""
        await self.flush_broadcasts()

    async def flush_broadcasts(self) -> None:
        """Post every broadcast this client owes, one failure not stopping the rest."""
        for broadcast in await run_db(pending_broadcasts):
            try:
                await self.deliver_broadcast(broadcast)
            except Exception:
                logger.exception('Failed to deliver broadcast %s', broadcast.pk)

    async def ask_question(self, interaction: discord.Interaction) -> bool:
        """Offer the unplayed questions of the library to the host."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            await run_db(require_host, guild, interaction.user)
            game = await run_db(active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            choices = await run_db(question_choices, game)
            if not choices:
                await interaction.followup.send(
                    'No unplayed question left. Add more questions in the admin.',
                    ephemeral=True)
                return False
            await interaction.followup.send(
                'Pick the question of the next round.', ephemeral=True,
                view=QueuePanel(self, choices))
        except PermissionError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to offer the questions')
            await interaction.followup.send(
                'Could not list the questions.', ephemeral=True)
            return False
        return True

    async def queue_question(self, interaction: discord.Interaction,
                             question: str, quiz_type: str = '') -> bool:
        """Pre-select the question of the next round."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            game = await run_db(active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            if not question.strip().isdigit():
                picked = await picked_pk(
                    lambda term: run_db(question_choices, game, term),
                    question)
                if picked is None:
                    await interaction.followup.send(
                        'That question no longer exists.', ephemeral=True)
                    return False
                question = str(picked)
            chosen = await run_db(question_by_pk, int(question))
            result = await run_db(create_round, game,
                                  interaction.user, chosen, quiz_type)
            await interaction.followup.send(
                round_note('Round {} queued for {}: {}.'.format(
                    result['index'], result['type_label'],
                    result['host_text']), result),
                ephemeral=True)
        except Question.DoesNotExist:
            await interaction.followup.send(
                'That question no longer exists.', ephemeral=True)
            return False
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to queue a question')
            await interaction.followup.send(
                'Could not queue the question.', ephemeral=True)
            return False
        return True

    async def unqueue_question(self, interaction: discord.Interaction,
                               question: str) -> bool:
        """Remove a queued question of the game."""
        if question.strip().isdigit():
            return await self.setup_flow(
                interaction, unqueue_questions, unqueued_text,
                [int(question)])
        await self.defer(interaction)
        guild = await guild_for(interaction)
        game = await run_db(active_game, guild)
        picked = (await picked_pk(
            lambda term: run_db(queued_choices, game, term), question)
            if game is not None else None)
        if picked is None:
            await interaction.followup.send('That question no longer exists.',
                                            ephemeral=True)
            return False
        return await self.setup_flow(
            interaction, unqueue_questions, unqueued_text, [picked],
            deferred=True)

    async def end_game(self, interaction: discord.Interaction) -> bool:
        """Close the game, publishing the round left open and the final scores."""
        await self.defer(interaction, update_panel=True)
        try:
            guild = await guild_for(interaction)
            game = await run_db(active_game, guild)
            if game is None:
                await self.respond(
                    interaction, NO_QUIZ_RUNNING,
                    closed=True)
                return False
            published = not game.is_preparing
            result, posts = await run_db(
                post_game_end, game, interaction.user, claim=True)
            if not published:
                # A game never published closes without a public recap.
                await self.respond(
                    interaction,
                    f'{result["game_name"]} was closed before being published.',
                    closed=True, panel=SetupPanel(self))
                return True
        except PermissionError:
            await self.respond(
                interaction, 'Only hosts of this server can end the game.')
            return False
        except Exception:
            logger.exception('Failed to end the game')
            await self.respond(interaction, 'Could not end the game.')
            return False
        # The game is over, so a post that fails must not deny it.
        await self.respond(interaction, f'{result["game_name"]} ended.',
                           closed=True)
        posted = []
        for broadcast, reveal in [post for post in posts
                                  if post[0].kind == Broadcast.Kind.REVEAL]:
            try:
                posted.extend(await self.post_broadcast(
                    broadcast, reveal, fallback=interaction.channel))
            except Exception:
                await interaction.followup.send(
                    'The last round was revealed, but its answer could not be '
                    'posted.', ephemeral=True)
        recap_posted = False
        for broadcast, recap in [post for post in posts
                                 if post[0].kind == Broadcast.Kind.RECAP]:
            try:
                posted.extend(await self.post_broadcast(
                    broadcast, recap, fallback=interaction.channel))
            except Exception:
                await interaction.followup.send(
                    'The final scores could not be posted.', ephemeral=True)
            else:
                recap_posted = True
        if recap_posted:
            await interaction.followup.send(
                posted_line('Final scores posted.', posted), ephemeral=True)
        return True

    async def respond(self, interaction: discord.Interaction, text: str,
                      closed: bool = False,
                      panel: discord.ui.View | None = None,
                      content: str | None = None) -> None:
        """Report an outcome, refreshing the panel it came from when asked."""
        if interaction.type is discord.InteractionType.component:
            view = panel or HostPanel(self)
            edit = {'view': disabled(view) if closed else view}
            if content is not None:
                edit['content'] = content
            try:
                await interaction.edit_original_response(**edit)
            except discord.HTTPException:
                logger.exception('Failed to update the host panel')
        await interaction.followup.send(text, ephemeral=True)

    def cache_form(self, message_id: int, form: dict) -> None:
        """Remember the guess form of a round, for its next Guess click."""
        self.forms[message_id] = form
        self.forms.move_to_end(message_id)
        while len(self.forms) > MAX_CACHED_FORMS:
            self.forms.popitem(last=False)

    async def open_guess_form(self, interaction: discord.Interaction) -> None:
        """Open the guess form of the round the clicked message belongs to."""
        message = interaction.message
        form = self.forms.get(message.id if message is not None else None)
        if form is not None:
            await interaction.response.send_modal(
                GuessModal(self, form, guess_form_title(interaction)))
            return
        await self.prime_form(interaction)

    async def prime_form(self, interaction: discord.Interaction) -> None:
        """Read the form of the round in play and offer it as a button.

        The form of that round was not primed in this process, which means the
        bot was reloaded since. The database is read after the defer, and the
        button hands the player the modal the cache could not.
        """
        message_id = interaction.message.id if interaction.message else 0
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            game = await run_db(active_game, guild)
            round_ = (await run_db(current_round, game)
                      if game is not None else None)
            if round_ is None:
                await interaction.followup.send(
                    'No round is running yet.', ephemeral=True)
                return
            display = await run_db(round_display, round_)
            self.cache_form(message_id, await run_db(guess_form, display))
            await interaction.followup.send(
                'Your guess form is ready.', ephemeral=True,
                view=GuessFormPanel(self, message_id))
        except Exception:
            logger.exception('Failed to open the guess form')
            await interaction.followup.send(
                'Could not open the guess form.', ephemeral=True)

    async def show_form(self, interaction: discord.Interaction,
                        message_id: int) -> None:
        """Open the guess form primed for a round message."""
        form = self.forms.get(message_id)
        if form is None:
            await interaction.response.send_message(
                'This form expired, click Guess again.', ephemeral=True)
            return
        await interaction.response.send_modal(
            GuessModal(self, form, guess_form_title(interaction)))

    async def record_guess(self, interaction: discord.Interaction,
                           answer: str = '', secondary_answer: str = '',
                           choice_pk: int | None = None) -> bool:
        """Record the guess of a player for the round in play."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            game = await run_db(active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            round_ = await run_db(current_round, game)
            if round_ is None:
                await interaction.followup.send(
                    'No round is running yet.', ephemeral=True)
                return False
            player = await player_for(interaction.user)
            if choice_pk is None:
                await run_db(submit_guess, round_, player, answer,
                             secondary_answer)
            else:
                await run_db(submit_multiple_choice, round_, player,
                             choice_pk, secondary_answer)
            await interaction.followup.send(
                f'Guess recorded for round {round_.index}. '
                'The result comes with the reveal.', ephemeral=True)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to record a guess')
            await interaction.followup.send(
                'Could not record the guess.', ephemeral=True)
            return False
        return True


async def setup(bot: commands.Bot) -> None:
    """Load the cog; called by the bot when the extension is loaded."""
    await bot.add_cog(GameCog(bot))
