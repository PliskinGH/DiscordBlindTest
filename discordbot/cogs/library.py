"""Slash commands hosts use to grow their server's question library."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from blindtest import services

from .. import embeds
from ..db import guild_for, run_db
from ..ui import option_label

logger = logging.getLogger(__name__)

# Value a host gives an option to drop the field it belongs to.
DROP_VALUE = '-'
# Hint the ``question edit`` options carry: an empty value keeps the field.
KEEP_HINT = ' Leave empty to keep it.'
DROP_HINT = f'{KEEP_HINT} {DROP_VALUE} to drop it.'


def given_fields(**values: str) -> dict[str, str]:
    """Return the options a host filled in, an omitted one left out.

    A ``-`` means the field is dropped, which the services read as an empty value.
    """
    return {field: '' if value.strip() == DROP_VALUE else value.strip()
            for field, value in values.items() if value.strip()}


async def question_autocomplete(
        interaction: discord.Interaction,
        current: str) -> list[app_commands.Choice[str]]:
    """Offer the questions of this server's own library."""
    try:
        guild = await guild_for(interaction)
        questions = await run_db(services.library_choices, guild, current)
        return [app_commands.Choice(name=option_label(choice),
                                    value=str(choice['pk']))
                for choice in questions]
    except Exception:
        logger.exception('Failed to offer question choices')
        return []


class LibraryCog(commands.Cog):
    """Answers and questions of this server's library."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name='library', description="Manage this server's question library.")
    answer = app_commands.Group(name='answer',
                                description='Answers of this server.',
                                parent=group)
    question = app_commands.Group(name='question',
                                  description='Questions of this server.',
                                  parent=group)
    variant = app_commands.Group(name='variant',
                                 description='Variants of an answer.',
                                 parent=group)

    @answer.command(name='add', description='Register an accepted answer text.')
    @app_commands.describe(text='Answer to accept, e.g. a song title.')
    async def answer_add(self, interaction: discord.Interaction,
                         text: str) -> None:
        """Save an answer in this server's library."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            answer = await run_db(services.add_answer, guild,
                                  interaction.user, text)
            await interaction.followup.send(
                f'Answer saved: **{answer.text}**.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to add an answer')
            await interaction.followup.send(
                'Could not save the answer.', ephemeral=True)

    @variant.command(name='add', description='Accept another text for an answer.')
    @app_commands.describe(answer='Answer the variant belongs to.',
                           text='Text to accept as well, e.g. Song (Remastered).')
    async def variant_add(self, interaction: discord.Interaction, answer: str,
                          text: str) -> None:
        """Register an accepted variant of an answer."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            saved = await run_db(services.add_variant, guild,
                                 interaction.user, answer, text)
            await interaction.followup.send(
                f'Variant saved: **{saved.text}**.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to add a variant')
            await interaction.followup.send(
                'Could not save the variant.', ephemeral=True)

    @variant.command(name='remove', description='Stop accepting a variant.')
    @app_commands.describe(answer='Answer the variant belongs to.',
                           text='Variant to drop.')
    async def variant_remove(self, interaction: discord.Interaction,
                             answer: str, text: str) -> None:
        """Drop a variant registered for an answer."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            await run_db(services.remove_variant, guild,
                         interaction.user, answer, text)
            await interaction.followup.send(
                f'Variant dropped: **{text.strip()}**.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to remove a variant')
            await interaction.followup.send(
                'Could not drop the variant.', ephemeral=True)

    @variant.command(name='list', description='Show the variants of an answer.')
    @app_commands.describe(answer='Answer to list the variants of.')
    async def variant_list(self, interaction: discord.Interaction,
                           answer: str) -> None:
        """Show the variants accepted for an answer."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            variants = await run_db(services.variants_of, guild,
                                    interaction.user, answer)
            shown = ', '.join(f'**{text}**' for text in variants)
            await interaction.followup.send(
                shown or 'This answer has no variant.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to list the variants')
            await interaction.followup.send(
                'Could not list the variants.', ephemeral=True)

    @question.command(name='add', description='Create a question for this server.')
    @app_commands.describe(answer='Required answer, e.g. the song title.',
                           artist='Second required answer, e.g. the artist.',
                           prompt='Text shown to the players.',
                           choices='Multiple choice options, separated by commas.',
                           year='Year of release.',
                           album='Album of the track.',
                           media='Link to the track, e.g. a YouTube URL.')
    async def question_add(self, interaction: discord.Interaction,
                           answer: str, artist: str = '', prompt: str = '',
                           choices: str = '', year: int | None = None,
                           album: str = '', media: str = '') -> None:
        """Create a quiz question tied to this server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            expected, expected_variants = services.split_answers(answer)
            secondary, secondary_variants = services.split_answers(artist)
            result = await run_db(
                services.add_question, guild, interaction.user, expected,
                prompt=prompt, secondary_text=secondary, year=year, album=album,
                media_url=media, choices=choices.split(','),
                expected_variants=expected_variants,
                secondary_variants=secondary_variants)
            saved = 'Question saved: **{}**.'.format(result['label'])
            if result['choices']:
                saved += ' It offers {} choices.'.format(result['choices'])
            if result['variants']:
                saved += ' It also accepts {} variant(s).'.format(
                    result['variants'])
            await interaction.followup.send(saved, ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to add a question')
            await interaction.followup.send(
                'Could not save the question.', ephemeral=True)

    @question.command(name='edit',
                      description='Change the fields of a question.')
    @app_commands.describe(
        question='Question to change.',
        answer='New answer, e.g. the song title.' + KEEP_HINT,
        artist='New second answer, e.g. the artist.' + DROP_HINT,
        prompt='New text shown to the players.' + DROP_HINT,
        choices=('New multiple choice options, separated by commas.'
                 + DROP_HINT),
        year='New year of release.' + DROP_HINT,
        album='New album of the track.' + DROP_HINT,
        media='New link to the track, e.g. a YouTube URL.' + DROP_HINT)
    @app_commands.autocomplete(question=question_autocomplete)
    async def question_edit(self, interaction: discord.Interaction,
                            question: str, answer: str = '', artist: str = '',
                            prompt: str = '', choices: str = '',
                            year: str = '', album: str = '',
                            media: str = '') -> None:
        """Change the fields of a question of this server's library."""
        await interaction.response.defer(ephemeral=True)
        if not question.isdigit():
            await interaction.followup.send('That question no longer exists.',
                                            ephemeral=True)
            return
        try:
            guild = await guild_for(interaction)
            result = await run_db(services.edit_question, guild,
                                  interaction.user, question,
                                  **given_fields(answer=answer,
                                                 artist=artist,
                                                 prompt=prompt,
                                                 choices=choices, year=year,
                                                 album=album, media=media))
            saved = 'Question updated: **{}**. Changed: {}.'.format(
                result['label'],
                ', '.join(f'`{field}`' for field in result['fields']))
            if result['media_url']:
                saved += ' It links to {}.'.format(
                    embeds.media_line(result['media_url']))
            await interaction.followup.send(saved, ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to edit a question')
            await interaction.followup.send(
                'Could not save the question.', ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Load the cog; called by the bot when the extension is loaded."""
    await bot.add_cog(LibraryCog(bot))