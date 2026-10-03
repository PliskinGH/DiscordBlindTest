"""Discord controls a quiz is played with.

Each control holds the cog and calls its operations, so the slash commands and
the controls share one implementation.
"""

from typing import TYPE_CHECKING

import discord

from blindtest.constants import CHOICE_NAME_LIMIT, MAX_CHOICES

if TYPE_CHECKING:
    from .cogs.game import GameCog

# Controls are persistent: the cog revives them at startup and Discord holds
# their custom_ids, so a bot restart must not change them.
GAME_ANSWER_ID = 'blindtest_player_answer'
HOST_NEXT_ID = 'blindtest_host_next'
HOST_REVEAL_ID = 'blindtest_host_reveal'
HOST_QUEUE_ID = 'blindtest_host_queue'
HOST_END_ID = 'blindtest_host_end'
QUEUE_PICK_ID = 'blindtest_queue_pick'
FORM_OPEN_ID = 'blindtest_form_open'
GUESS_PICK_ID = 'blindtest_answer_pick'
GUESS_TEXT_ID = 'blindtest_answer_text'
GUESS_SECONDARY_ID = 'blindtest_answer_secondary'
GUESS_MODAL_ID = 'blindtest_answer_form'
SETUP_ADD_ID = 'blindtest_setup_add'
SETUP_REMOVE_ID = 'blindtest_setup_remove'
SETUP_COPY_ID = 'blindtest_setup_copy'
SETUP_PUBLISH_ID = 'blindtest_setup_publish'
SETUP_CLEAR_ID = 'blindtest_setup_clear'
SETUP_END_ID = 'blindtest_setup_end'

# Panel labels.
END_QUIZ_LABEL = 'End the quiz'


def pick_max(choices: 'list[dict] | None') -> int:
    """Return how many options a picker may select at once."""
    return max(1, min(MAX_CHOICES, len(choices or [])))


def option_label(choice: dict) -> str:
    """Return the label of a picker option, flagged for media and clipped."""
    prefix = '🎵 ' if choice.get('media') else ''
    return (prefix + choice['label'])[:CHOICE_NAME_LIMIT]


def pick_options(choices: 'list[dict] | None') -> list[discord.SelectOption]:
    """Return a picker's options, offering a placeholder when it has none."""
    if not choices:
        return [discord.SelectOption(label='Nothing to pick here',
                                     value='none')]
    return [discord.SelectOption(label=option_label(choice),
                                 value=str(choice['pk']))
            for choice in choices]


class GuessModal(discord.ui.Modal):
    """Answer form of a round, built from the form the services returned.

    A multiple choice round offers its choices in a select and keeps the text
    field for the secondary answer; every other round asks for two texts.
    """

    def __init__(self, cog: 'GameCog', form: dict) -> None:
        super().__init__(title='Your answer', timeout=None,
                         custom_id=GUESS_MODAL_ID)
        self.cog = cog
        self.form = form
        if form['options']:
            self.pick = discord.ui.Select(
                custom_id=GUESS_PICK_ID,
                placeholder='Pick your answer',
                options=[discord.SelectOption(label=option_label(option),
                                              value=str(option['pk']))
                         for option in form['options']])
            self.add_item(discord.ui.Label(text='Answer', component=self.pick))
            self.answer_input = None
        else:
            self.answer_input = discord.ui.TextInput(
                max_length=200, custom_id=GUESS_TEXT_ID,
                placeholder='Song title, or whatever the question asks for.')
            self.add_item(discord.ui.Label(text='Answer',
                                           component=self.answer_input))
            self.pick = None
        self.secondary_input = discord.ui.TextInput(
            max_length=200, required=False, custom_id=GUESS_SECONDARY_ID,
            placeholder='Artist for blind tests, or secondary required answer.')
        self.add_item(discord.ui.Label(text='Secondary answer',
                                       component=self.secondary_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Record the answer of the round in play."""
        # Read the values before the first await: they belong to this submission.
        picked = int(self.pick.values[0]) if self.pick is not None else None
        answer = self.answer_input.value if self.answer_input is not None else ''
        secondary = self.secondary_input.value
        await self.cog.record_guess(interaction, answer, secondary, picked)


class AnswerFormPanel(discord.ui.View):
    """Private button opening the answer form primed for a round message."""

    def __init__(self, cog: 'GameCog', message_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        button = discord.ui.Button(label='Answer', style=discord.ButtonStyle.primary,
                                   custom_id=f'{FORM_OPEN_ID}:{message_id}')
        button.callback = self.open_form
        self.message_id = message_id
        self.add_item(button)

    async def open_form(self, interaction: discord.Interaction) -> None:
        """Open the form of the round this panel was primed with."""
        await self.cog.show_form(interaction, self.message_id)


class GamePanel(discord.ui.View):
    """Answer button posted with every round."""

    def __init__(self, cog: 'GameCog') -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label='Answer', style=discord.ButtonStyle.primary,
                       custom_id=GAME_ANSWER_ID)
    async def answer(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        """Open the answer form of the round in play."""
        await self.cog.open_guess_form(interaction)


class HostPanel(discord.ui.View):
    """Private controls of the running game, held by the host alone."""

    def __init__(self, cog: 'GameCog') -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label='Next round', style=discord.ButtonStyle.primary,
                       custom_id=HOST_NEXT_ID)
    async def next_round(self, interaction: discord.Interaction,
                         button: discord.ui.Button) -> None:
        """Open the next round."""
        await self.cog.open_next_round(interaction)

    @discord.ui.button(label='Reveal', style=discord.ButtonStyle.secondary,
                       custom_id=HOST_REVEAL_ID)
    async def reveal(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        """Reveal the round in play."""
        await self.cog.reveal_round(interaction)

    @discord.ui.button(label='Queue a question', style=discord.ButtonStyle.secondary,
                       custom_id=HOST_QUEUE_ID)
    async def queue(self, interaction: discord.Interaction,
                    button: discord.ui.Button) -> None:
        """Offer the unplayed questions of the library."""
        await self.cog.ask_question(interaction)

    @discord.ui.button(label=END_QUIZ_LABEL, style=discord.ButtonStyle.danger,
                       custom_id=HOST_END_ID)
    async def end_game(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        """Close the game and publish the final scores."""
        await self.cog.end_game(interaction)


class QuestionSelect(discord.ui.Select):
    """Question picker of the host panel."""

    def __init__(self, cog: 'GameCog',
                 choices: 'list[dict] | None' = None) -> None:
        super().__init__(placeholder='Question of the next round',
                         custom_id=QUEUE_PICK_ID,
                         options=[discord.SelectOption(label=option_label(choice),
                                                       value=str(choice['pk']))
                                  for choice in choices or []])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        """Queue the picked question for the next round."""
        await self.cog.queue_question(interaction, self.values[0])


class QueuePanel(discord.ui.View):
    """Private question picker offered to the host."""

    def __init__(self, cog: 'GameCog',
                 choices: 'list[dict] | None' = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(QuestionSelect(cog, choices))


class QuestionPickSelect(discord.ui.Select):
    """Questions to add to the game being prepared."""

    def __init__(self, cog: 'GameCog',
                 choices: 'list[dict] | None' = None) -> None:
        super().__init__(placeholder='Add questions to the game',
                         custom_id=SETUP_ADD_ID,
                         max_values=pick_max(choices),
                         options=pick_options(choices),
                         disabled=not choices)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        """Queue the questions the host picked."""
        await self.cog.queue_selection(interaction, self.values)


class QueuedPickSelect(discord.ui.Select):
    """Queued questions to remove from the game being prepared."""

    def __init__(self, cog: 'GameCog',
                 choices: 'list[dict] | None' = None) -> None:
        super().__init__(placeholder='Remove queued questions',
                         custom_id=SETUP_REMOVE_ID,
                         max_values=pick_max(choices),
                         options=pick_options(choices),
                         disabled=not choices)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        """Drop the queued questions the host picked."""
        await self.cog.drop_selection(interaction, self.values)


class GamePickSelect(discord.ui.Select):
    """Games whose questions the host can copy."""

    def __init__(self, cog: 'GameCog',
                 choices: 'list[dict] | None' = None) -> None:
        super().__init__(placeholder='Copy the questions of a game',
                         custom_id=SETUP_COPY_ID,
                         options=pick_options(choices),
                         disabled=not choices)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        """Copy the questions of the game the host picked."""
        await self.cog.copy_selection(interaction, self.values[0])


class SetupPanel(discord.ui.View):
    """Private controls of the game being prepared."""

    def __init__(self, cog: 'GameCog', choices: 'list[dict] | None' = None,
                 queued: 'list[dict] | None' = None,
                 games: 'list[dict] | None' = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(QuestionPickSelect(cog, choices))
        self.add_item(QueuedPickSelect(cog, queued))
        self.add_item(GamePickSelect(cog, games))
        publish = discord.ui.Button(label='Publish the game',
                                    style=discord.ButtonStyle.success,
                                    custom_id=SETUP_PUBLISH_ID, row=3)
        publish.callback = self.publish
        clear = discord.ui.Button(label='Clear the queue',
                                  style=discord.ButtonStyle.secondary,
                                  custom_id=SETUP_CLEAR_ID, row=3)
        clear.callback = self.clear
        self.add_item(publish)
        self.add_item(clear)
        end = discord.ui.Button(label=END_QUIZ_LABEL,
                                style=discord.ButtonStyle.danger,
                                custom_id=SETUP_END_ID, row=3)
        end.callback = self.end_game
        self.add_item(end)

    async def publish(self, interaction: discord.Interaction) -> None:
        """Publish the game being prepared."""
        await self.cog.publish(interaction)

    async def clear(self, interaction: discord.Interaction) -> None:
        """Empty the queue of the game being prepared."""
        await self.cog.clear(interaction)

    async def end_game(self, interaction: discord.Interaction) -> None:
        """Close the game being prepared without publishing it."""
        await self.cog.end_game(interaction)


def disabled(view: discord.ui.View) -> discord.ui.View:
    """Return a copy of a panel with every control disabled."""
    for item in view.children:
        item.disabled = True
    return view

