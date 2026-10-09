"""Slash commands hosts use to grow their server's question library."""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from blindtest.services.guilds import require_host
from blindtest.services.library import (add_answer, add_question, add_variant,
                                        edit_question, library_choices,
                                        remove_variant, set_show_all_questions,
                                        split_answers, variants_of)


from .. import embeds
from ..db import guild_for, player_for, run_db
from ..ui import option_label, picked_pk

logger = logging.getLogger(__name__)

# Value a host gives an option to clear the field it belongs to.
CLEAR_VALUE = '-'
# Hint the ``question edit`` options carry: an empty value keeps the field.
KEEP_HINT = ' Leave empty to keep it.'
CLEAR_HINT = f'{KEEP_HINT} {CLEAR_VALUE} to clear it.'


def given_fields(**values: str) -> dict[str, str]:
    """Return the options a host filled in, an omitted one left out.

    A ``-`` clears the field, which the services read as an empty value.
    """
    return {field: '' if value.strip() == CLEAR_VALUE else value.strip()
            for field, value in values.items() if value.strip()}


async def question_autocomplete(
        interaction: discord.Interaction,
        current: str) -> list[app_commands.Choice[str]]:
    """Offer the questions of this server's own library the host may see."""
    try:
        guild = await guild_for(interaction)
        viewer = await player_for(interaction.user)
        questions = await run_db(library_choices, guild, viewer, current)
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
            answer = await run_db(add_answer, guild,
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
            saved = await run_db(add_variant, guild,
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
                           text='Variant to remove.')
    async def variant_remove(self, interaction: discord.Interaction,
                             answer: str, text: str) -> None:
        """Remove a variant registered for an answer."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            await run_db(remove_variant, guild,
                         interaction.user, answer, text)
            await interaction.followup.send(
                f'Variant removed: **{text.strip()}**.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to remove a variant')
            await interaction.followup.send(
                'Could not remove the variant.', ephemeral=True)

    @variant.command(name='list', description='Show the variants of an answer.')
    @app_commands.describe(answer='Answer to list the variants of.')
    async def variant_list(self, interaction: discord.Interaction,
                           answer: str) -> None:
        """Show the variants accepted for an answer."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            variants = await run_db(variants_of, guild,
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
        """Create a game question tied to this server."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            expected, expected_variants = split_answers(answer)
            secondary, secondary_variants = split_answers(artist)
            result = await run_db(
                add_question, guild, interaction.user, expected,
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
        artist='New second answer, e.g. the artist.' + CLEAR_HINT,
        prompt='New text shown to the players.' + CLEAR_HINT,
        choices=('New multiple choice options, separated by commas.'
                 + CLEAR_HINT),
        year='New year of release.' + CLEAR_HINT,
        album='New album of the track.' + CLEAR_HINT,
        media='New link to the track, e.g. a YouTube URL.' + CLEAR_HINT)
    @app_commands.autocomplete(question=question_autocomplete)
    async def question_edit(self, interaction: discord.Interaction,
                            question: str, answer: str = '', artist: str = '',
                            prompt: str = '', choices: str = '',
                            year: str = '', album: str = '',
                            media: str = '') -> None:
        """Change the fields of a question of this server's library."""
        await interaction.response.defer(ephemeral=True)
        if not question.strip().isdigit():
            guild = await guild_for(interaction)
            viewer = await player_for(interaction.user)
            picked = await picked_pk(
                lambda term: run_db(library_choices, guild, viewer, term),
                question)
            if picked is None:
                await interaction.followup.send(
                    'That question no longer exists.', ephemeral=True)
                return
            question = str(picked)
        try:
            guild = await guild_for(interaction)
            result = await run_db(edit_question, guild,
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

    @group.command(
        name='spoilers',
        description='See every question of this server, or only yours.')
    @app_commands.describe(
        show='Show every question of the server, including the ones other '
             'hosts authored.')
    async def spoilers(self, interaction: discord.Interaction,
                       show: bool) -> None:
        """Keep the questions of the other hosts hidden, or read them all."""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await guild_for(interaction)
            await run_db(require_host, guild, interaction.user)
            viewer = await player_for(interaction.user)
            await run_db(set_show_all_questions, viewer, show)
            await interaction.followup.send(
                'You now see every question of this server.' if show else
                'You now only see the questions you authored.', ephemeral=True)
        except (PermissionError, ValueError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception('Failed to set the spoiler option')
            await interaction.followup.send(
                'Could not change what you see.', ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Load the cog; called by the bot when the extension is loaded."""
    await bot.add_cog(LibraryCog(bot))
