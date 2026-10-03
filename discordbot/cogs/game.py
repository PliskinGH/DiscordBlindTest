"""Slash commands and the operations behind the controls of a quiz."""

import logging
from collections import OrderedDict

import discord
from discord import app_commands
from discord.ext import commands

from blindtest import services
from blindtest.models import Game, Question, QuizType, ScoringMode

from .. import embeds
from ..db import guild_for, player_for, run_db
from ..ui import (AnswerFormPanel, GamePanel, GuessModal, HostPanel, QueuePanel,
                  SetupPanel, disabled, option_label)

logger = logging.getLogger(__name__)

# Answer forms primed by the rounds opened in this process.
MAX_CACHED_FORMS = 20

# The channels Discord offers for a game: any channel or thread of the server.
GameChannel = discord.abc.GuildChannel | discord.Thread

# Said wherever a command needs the game of the server and finds none.
NO_QUIZ_RUNNING = 'No quiz is running in this server.'

# Descriptions both groups share; only setup, queue and next differ.
PUBLISH_DESCRIPTION = 'Publish the game being prepared.'
PANEL_DESCRIPTION = 'Reopen the private controls of the running game.'
GUESS_DESCRIPTION = 'Submit your answer for the round in play.'
END_DESCRIPTION = 'End the quiz of this server.'
REVEAL_DESCRIPTION = 'Reveal the current round.'
UNQUEUE_DESCRIPTION = 'Drop a question queued for a round.'
CLEAR_DESCRIPTION = 'Drop every queued question of the game.'
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
UNQUEUE_OPTIONS = {'question': 'Queued question to drop.'}
COPY_OPTIONS = {'source': 'Game to copy the questions from.'}


def game_channel(interaction: discord.Interaction,
                 game: Game) -> discord.abc.Messageable:
    """Return the channel of a game, or the invoking one when it is gone."""
    client = interaction.client
    guild = interaction.guild
    channel = (client.get_channel(game.channel_id)
               or (guild.get_channel(game.channel_id) if guild else None))
    return channel or interaction.channel


def named_channel_id(interaction: discord.Interaction,
                     channel: GameChannel | None) -> int | None:
    """Return the ID of the channel the host named, or None when it named none.

    The order of preference between the channels of a game is the domain's, in
    ``services.target_channel_id``; this only checks that the named channel is
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
    naming it, which would swallow the announcement and every round.
    """
    if role.guild.id != interaction.guild.id:
        raise ValueError("That role belongs to another server.")
    if role.position >= interaction.guild.me.top_role.position:
        raise ValueError("That role is above me: I cannot ping it.")


def named_ping_role_id(interaction: discord.Interaction,
                       role: discord.Role | None) -> int | None:
    """Return the ID of the role the host named, or None when it named none.

    Which role a game pings is the domain's prerogative, in
    ``services.target_ping_role_id``; this only checks that the bot may use it.
    """
    if role is None:
        return None
    require_pingable(interaction, role)
    return role.id


def ping_text(payload: dict) -> tuple[str, list[int]]:
    """Return the role ping formatted for discord."""
    role = payload.get('ping_role_id')
    return (f'<@&{role}>', [role]) if role else ('', [])


def setup_text(data: dict) -> str:
    """Return the line naming the game being prepared and its queue."""
    return ('{}: {} question(s) queued. Add, drop or copy questions, then '
            'publish.').format(data['game_name'], data['queued'])


def added_text(result: dict) -> str:
    """Return how a queueing operation reports what it queued."""
    return '{} question(s) added, {} skipped.'.format(result['added'],
                                                      result['skipped'])


def dropped_text(dropped: int) -> str:
    """Return how a drop operation reports what it dropped."""
    return '{} question(s) dropped.'.format(dropped)


def unqueued_text(dropped: int) -> str:
    """Return how dropping one queued question reports itself."""
    return dropped_text(dropped) if dropped else 'That question is not queued.'


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
        game = await run_db(services.active_game, guild)
        if game is None:
            return []
        questions = await run_db(services.question_choices, game, current)
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
        game = await run_db(services.active_game, guild)
        if game is None:
            return []
        queued = await run_db(services.queued_choices, game, current)
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
        games = await run_db(services.game_choices, guild, None, current)
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

    def controls(self) -> list[discord.ui.View]:
        """Return the controls a restart has to revive, as they are stateless."""
        return [GamePanel(self), HostPanel(self), QueuePanel(self), SetupPanel(self)]

    async def defer(self, interaction: discord.Interaction,
                    update_panel: bool = False) -> None:
        """Answer the interaction before any database work.

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
            games = await run_db(services.games_count, guild)
            await interaction.followup.send(
                f'Pong! {self.bot.latency * 1000:.0f} ms - '
                f'{games} game(s) in this server.',
                ephemeral=True)
        except Exception:
            logger.exception('Failed to process ping command')
            await interaction.followup.send('Failed to check database status.', ephemeral=True)

    @quiz.command(name='setup', description='Set up a quiz in a channel.')
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
                       description='Set up a blind test quiz in a channel.')
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
        """Record the answer of the player invoking the command."""
        await self.record_guess(interaction, answer, secondary_answer)

    @blindtest.command(name='guess', description=GUESS_DESCRIPTION)
    @app_commands.describe(**GUESS_OPTIONS)
    async def blindtest_guess_command(self, interaction: discord.Interaction,
                                      answer: str,
                                      secondary_answer: str = '') -> None:
        """Record the answer of the player invoking the command."""
        await self.record_guess(interaction, answer, secondary_answer)

    @quiz.command(name='end', description=END_DESCRIPTION)
    async def quiz_end_command(self, interaction: discord.Interaction) -> None:
        """Close the running game and publish the final scores."""
        await self.end_game(interaction)

    @blindtest.command(name='end', description=END_DESCRIPTION)
    async def blindtest_end_command(self, interaction: discord.Interaction) -> None:
        """Close the running game and publish the final scores."""
        await self.end_game(interaction)

    @quiz.command(name='next', description='Open the next round of the quiz.')
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
        """Drop a queued question of the game."""
        await self.unqueue_question(interaction, question)

    @blindtest.command(name='unqueue', description=UNQUEUE_DESCRIPTION)
    @app_commands.describe(**UNQUEUE_OPTIONS)
    @app_commands.autocomplete(question=queued_autocomplete)
    async def blindtest_unqueue_command(self, interaction: discord.Interaction,
                                        question: str) -> None:
        """Drop a queued question of the game."""
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


    async def setup_game(self, interaction: discord.Interaction, scoring: str,
                         name: str = '', quiz_type: str = '',
                         channel: GameChannel | None = None,
                         role: discord.Role | None = None) -> bool:
        """Set up a new game and hand its setup controls to the host."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            channel_id = named_channel_id(interaction, channel)
            ping_role_id = named_ping_role_id(interaction, role)
            game = await run_db(services.create_game, guild, channel_id,
                                interaction.user, scoring, name=name,
                                quiz_type=quiz_type or QuizType.BLIND_TEST,
                                state=Game.State.SETUP,
                                invoking_id=interaction.channel_id,
                                ping_role_id=ping_role_id)
            data = await run_db(services.panel_data, game)
            await self.send_setup_panel(interaction, data)
        except PermissionError:
            await interaction.followup.send(
                'Only hosts of this server can set up a quiz.',
                ephemeral=True)
            return False
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to set up quiz')
            await interaction.followup.send(
                'Could not set up the quiz (a game may already be running '
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
                               data: dict) -> None:
        """Send the caller the controls of the game being prepared."""
        await interaction.followup.send(setup_text(data), ephemeral=True,
                                        view=self.setup_view(data))

    async def show_panel(self, interaction: discord.Interaction) -> bool:
        """Send the controls of the running game to the host asking for them."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            await run_db(services.require_host, guild, interaction.user)
            game = await run_db(services.active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            if game.is_preparing:
                await self.send_setup_panel(
                    interaction, await run_db(services.panel_data, game))
            else:
                await self.send_panel(interaction, 'Quiz controls.')
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
                         text_of, *args) -> bool:
        """Run an operation on the game and refresh the panel it shows."""
        await self.defer(interaction, update_panel=True)
        try:
            guild = await guild_for(interaction)
            game = await run_db(services.active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            result = await run_db(operation, game, interaction.user, *args)
            data = await run_db(services.panel_data, game)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to update the quiz')
            await interaction.followup.send(
                'Could not update the quiz.', ephemeral=True)
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
            interaction, services.queue_questions, added_text,
            [int(value) for value in values])

    async def drop_selection(self, interaction: discord.Interaction,
                             values: 'list[str]') -> bool:
        """Drop the queued questions the host picked."""
        return await self.setup_flow(
            interaction, services.unqueue_questions, dropped_text,
            [int(value) for value in values])

    async def copy_selection(self, interaction: discord.Interaction,
                             value: str) -> bool:
        """Queue the questions of the game the host picked."""
        return await self.setup_flow(
            interaction, services.copy_questions_by_pk, added_text, int(value))

    async def clear(self, interaction: discord.Interaction) -> bool:
        """Empty the queue of the game."""
        return await self.setup_flow(interaction, services.clear_queue,
                                     dropped_text)

    async def copy_questions(self, interaction: discord.Interaction,
                             source: str) -> bool:
        """Queue the questions of another game."""
        if not source.isdigit():
            await self.defer(interaction)
            await interaction.followup.send('That game no longer exists.',
                                            ephemeral=True)
            return False
        return await self.copy_selection(interaction, source)

    async def publish(self, interaction: discord.Interaction) -> bool:
        """Publish the game being prepared and announce it."""
        await self.defer(interaction, update_panel=True)
        try:
            guild = await guild_for(interaction)
            game = await run_db(services.active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            result = await run_db(services.publish_game, game, interaction.user)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to publish the quiz')
            await interaction.followup.send(
                'Could not publish the quiz.', ephemeral=True)
            return False
        text = '{} is published.'.format(result['game_name'])
        if interaction.type is discord.InteractionType.component:
            await self.respond(interaction, text, panel=HostPanel(self),
                               content='{} is live. Host controls:'.format(
                                   result['game_name']))
        else:
            await interaction.followup.send(text, ephemeral=True,
                                            view=HostPanel(self))
        # The game is live, so an announcement that fails must not deny it.
        ping, mentions = ping_text(result)
        try:
            await embeds.post(game_channel(interaction, game), content=ping,
                              embeds=[embeds.announce_embed(
                                  result, interaction.user.mention)],
                              mentions=mentions)
        except Exception:
            logger.exception('Failed to post the announcement')
            await interaction.followup.send(
                'The announcement could not be posted.', ephemeral=True)
        return True

    async def open_next_round(self, interaction: discord.Interaction) -> bool:
        """Open the next round and post its Answer button."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            game = await run_db(services.active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            result = await run_db(services.start_round, game, interaction.user)
            ping, mentions = ping_text(result)
            message = await embeds.post(game_channel(interaction, game),
                                        content=ping,
                                        embeds=[embeds.round_embed(result)],
                                        view=GamePanel(self), mentions=mentions)
            self.cache_form(message.id, result['form'])
            await interaction.followup.send(
                round_note('Round {} opened: {}.'.format(
                    result['index'], result['host_text']), result),
                ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to open the next round')
            await interaction.followup.send(
                'Could not open the next round.', ephemeral=True)
            return False
        return True

    async def reveal_round(self, interaction: discord.Interaction) -> bool:
        """Close the round and publish its answer with the standings."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            game = await run_db(services.active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            round_ = await run_db(services.current_round, game)
            if round_ is None:
                await interaction.followup.send(
                    'No round is running yet.', ephemeral=True)
                return False
            result = await run_db(services.reveal_round, round_, interaction.user)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to reveal the round')
            await interaction.followup.send(
                'Could not reveal the round.', ephemeral=True)
            return False
        try:
            await self.post_reveal(game_channel(interaction, game), result)
        except Exception:
            logger.exception('Failed to publish the revealed round')
            await interaction.followup.send(
                'Round {} revealed, but its answer could not be published.'.format(
                    result['index']), ephemeral=True)
            return False
        await interaction.followup.send(
            'Round {} revealed.'.format(result['index']), ephemeral=True)
        return True

    async def post_reveal(self, channel: discord.abc.Messageable,
                          reveal: dict) -> None:
        """Publish the answer of a revealed round with the standings."""
        await embeds.post(channel, embeds=[embeds.reveal_embed(reveal)])
        if reveal['scores']:
            await embeds.post(channel, embeds=[embeds.scores_embed(
                reveal, f'Round {reveal["index"]} scores',
                embeds.leader_line(reveal['scores'], 'is currently winning'))])

    async def ask_question(self, interaction: discord.Interaction) -> bool:
        """Offer the unplayed questions of the library to the host."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            await run_db(services.require_host, guild, interaction.user)
            game = await run_db(services.active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            choices = await run_db(services.question_choices, game)
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
            game = await run_db(services.active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            if not question.isdigit():
                await interaction.followup.send(
                    'That question no longer exists.', ephemeral=True)
                return False
            chosen = await run_db(services.question_by_pk, int(question))
            result = await run_db(services.create_round, game,
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
        """Drop a queued question of the game."""
        if not question.isdigit():
            await self.defer(interaction)
            await interaction.followup.send('That question no longer exists.',
                                            ephemeral=True)
            return False
        return await self.setup_flow(
            interaction, services.unqueue_questions, unqueued_text,
            [int(question)])

    async def end_game(self, interaction: discord.Interaction) -> bool:
        """Close the game, publishing the round left open and the final scores."""
        await self.defer(interaction, update_panel=True)
        try:
            guild = await guild_for(interaction)
            game = await run_db(services.active_game, guild)
            if game is None:
                await self.respond(
                    interaction, NO_QUIZ_RUNNING,
                    closed=True)
                return False
            announced = not game.is_preparing
            result = await run_db(services.finish_game, game, interaction.user)
            if not announced:
                # A game never announced closes without a public recap.
                await self.respond(
                    interaction,
                    f'{result["game_name"]} was closed before being published.',
                    closed=True, panel=SetupPanel(self))
                return True
        except PermissionError:
            await self.respond(
                interaction, 'Only hosts of this server can end the quiz.')
            return False
        except Exception:
            logger.exception('Failed to end the quiz')
            await self.respond(interaction, 'Could not end the quiz.')
            return False
        # The game is over, so a post that fails must not deny it.
        await self.respond(interaction, f'{result["game_name"]} ended.', closed=True)
        channel = game_channel(interaction, game)
        if result['reveal'] is not None:
            try:
                await self.post_reveal(channel, result['reveal'])
            except Exception:
                logger.exception('Failed to publish the revealed round')
                await interaction.followup.send(
                    'The last round was revealed, but its answer could not be '
                    'posted.', ephemeral=True)
        try:
            await embeds.post(channel, embeds=[embeds.recap_embed(result)])
        except Exception:
            logger.exception('Failed to publish the final scores')
            await interaction.followup.send(
                'The final scores could not be posted.', ephemeral=True)
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
        """Remember the answer form of a round, for its next Answer click."""
        self.forms[message_id] = form
        self.forms.move_to_end(message_id)
        while len(self.forms) > MAX_CACHED_FORMS:
            self.forms.popitem(last=False)

    async def open_guess_form(self, interaction: discord.Interaction) -> None:
        """Open the answer form of the round the clicked message belongs to."""
        message = interaction.message
        form = self.forms.get(message.id if message is not None else None)
        if form is not None:
            await interaction.response.send_modal(GuessModal(self, form))
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
            game = await run_db(services.active_game, guild)
            round_ = (await run_db(services.current_round, game)
                      if game is not None else None)
            if round_ is None:
                await interaction.followup.send(
                    'No round is running yet.', ephemeral=True)
                return
            self.cache_form(message_id,
                            await run_db(services.guess_form, round_))
            await interaction.followup.send(
                'Your answer form is ready.', ephemeral=True,
                view=AnswerFormPanel(self, message_id))
        except Exception:
            logger.exception('Failed to open the answer form')
            await interaction.followup.send(
                'Could not open the answer form.', ephemeral=True)

    async def show_form(self, interaction: discord.Interaction,
                        message_id: int) -> None:
        """Open the answer form primed for a round message."""
        form = self.forms.get(message_id)
        if form is None:
            await interaction.response.send_message(
                'This form expired, click Answer again.', ephemeral=True)
            return
        await interaction.response.send_modal(GuessModal(self, form))

    async def record_guess(self, interaction: discord.Interaction,
                           answer: str = '', secondary_answer: str = '',
                           choice_pk: int | None = None) -> bool:
        """Record the answer of a player for the round in play."""
        await self.defer(interaction)
        try:
            guild = await guild_for(interaction)
            game = await run_db(services.active_game, guild)
            if game is None:
                await interaction.followup.send(
                    NO_QUIZ_RUNNING, ephemeral=True)
                return False
            round_ = await run_db(services.current_round, game)
            if round_ is None:
                await interaction.followup.send(
                    'No round is running yet.', ephemeral=True)
                return False
            player = await player_for(interaction.user)
            if choice_pk is None:
                await run_db(services.submit_guess, round_, player, answer,
                             secondary_answer)
            else:
                await run_db(services.submit_multiple_choice, round_, player,
                             choice_pk, secondary_answer)
            await interaction.followup.send(
                f'Answer recorded for round {round_.index}. '
                'The result comes with the reveal.', ephemeral=True)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        except Exception:
            logger.exception('Failed to record an answer')
            await interaction.followup.send(
                'Could not record the answer.', ephemeral=True)
            return False
        return True


async def setup(bot: commands.Bot) -> None:
    """Load the cog; called by the bot when the extension is loaded."""
    await bot.add_cog(GameCog(bot))
