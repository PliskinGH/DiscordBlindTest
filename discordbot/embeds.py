"""Public embeds of a game, clipped to what Discord accepts.

Discord refuses an oversized embed with a 400 that reaches the host as a failed
flow, and discord.py validates nothing in advance. Every public message goes
through :func:`post`, which trims the content and the embeds first.
"""

from collections.abc import Iterable

import discord

from blindtest.constants import MAX_LISTED_PLAYERS
from blindtest.models import QuizType

# Message and embed limits, from Discord's documentation.
CONTENT_LIMIT = 2000
TOTAL_LIMIT = 6000
EMBED_LIMIT = 10
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_LIMIT = 25
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
AUTHOR_LIMIT = 256
# Kept free for the "and N more" notes and the line separators.
RESERVE = 80

RUNNING = discord.Colour.blurple()
REVEALED = discord.Colour.gold()
SCORED = discord.Colour.green()
FINISHED = discord.Colour.dark_grey()
MEDALS = ('🥇', '🥈', '🥉')
TYPE_ICONS = {QuizType.BLIND_TEST.value: '🎵', QuizType.OPEN.value: '❓',
              QuizType.MULTIPLE_CHOICE.value: '🔢'}


def clip(text: str, limit: int) -> str:
    """Return text trimmed to a limit, ending with an ellipsis."""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '…'


def size(embed: discord.Embed) -> int:
    """Return the number of characters Discord counts for an embed."""
    total = len(embed.title or '') + len(embed.description or '')
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name) + len(field.value)
    return total


def fit(embed: discord.Embed, budget: int = TOTAL_LIMIT) -> discord.Embed:
    """Trim an embed to the limits, dropping fields the budget cannot hold."""
    if embed.title:
        embed.title = clip(embed.title, TITLE_LIMIT)
    if embed.description:
        embed.description = clip(embed.description, DESCRIPTION_LIMIT)
    if embed.footer and embed.footer.text:
        embed.set_footer(text=clip(embed.footer.text, FOOTER_LIMIT),
                         icon_url=embed.footer.icon_url)
    if embed.author and embed.author.name:
        embed.set_author(name=clip(embed.author.name, AUTHOR_LIMIT),
                         icon_url=embed.author.icon_url, url=embed.author.url)
    fields = [(clip(field.name, FIELD_NAME_LIMIT),
               clip(field.value, FIELD_VALUE_LIMIT), field.inline)
              for field in list(embed.fields)[:FIELD_LIMIT]]
    embed.clear_fields()
    used = size(embed)
    for name, value, inline in fields:
        if embed.fields and used + len(name) + len(value) > budget - RESERVE:
            break
        embed.add_field(name=name, value=value, inline=inline)
        used += len(name) + len(value)
    return embed


def fit_all(embeds: Iterable[discord.Embed],
            budget: int = TOTAL_LIMIT) -> list[discord.Embed]:
    """Trim a list of embeds so the whole message stays within the budget."""
    kept: list[discord.Embed] = []
    for embed in list(embeds)[:EMBED_LIMIT]:
        left = budget - sum(size(kept_embed) for kept_embed in kept)
        if left <= 0:
            break
        kept.append(fit(embed, left))
    return kept


async def post(channel: discord.abc.Messageable, *, content: str | None = None,
               embeds: Iterable[discord.Embed] = (),
               view: discord.ui.View | None = None,
               mentions: Iterable[int] = ()) -> discord.Message:
    """Send a public message Discord accepts, whatever it holds.

    ``mentions`` names the roles ``content`` may ping, so no message can
    reach a role it was not built for.
    """
    fitted = fit_all(embeds)
    extra = {'view': view} if view is not None else {}
    if mentions:
        extra['allowed_mentions'] = discord.AllowedMentions(roles=list(mentions))
    return await channel.send(
        clip(content, CONTENT_LIMIT) if content else None,
        embeds=fitted, **extra)


def icon(quiz_type: str) -> str:
    """Return the icon shown for a quiz type."""
    return TYPE_ICONS.get(quiz_type, '❔')


def row_label(row: dict) -> str:
    """Return the name a score row shows, be it a player or a team."""
    if 'name' in row:
        return row['name']
    return row['discord_name'] or row['username']


def score_lines(rows: list[dict], limit: int) -> tuple[list[str], int]:
    """Return the score lines that fit a limit, and how many rows are left."""
    lines: list[str] = []
    used = 0
    for rank, row in enumerate(rows, start=1):
        medal = MEDALS[rank - 1] if rank <= len(MEDALS) else f'{rank}.'
        line = f'{medal} {row_label(row)} — {row["points"]} pt'
        if lines and used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    return lines, len(rows) - len(lines)


def more(left: int, noun: str) -> str:
    """Return the note telling how many rows an embed could not show."""
    return f'…and {left} more {noun}.' if left else ''


def block(lines: list[str], left: int = 0, noun: str = 'players') -> str:
    """Return the lines of a scoreboard, with its overflow note."""
    note = more(left, noun)
    return '\n'.join([*lines, note] if note else lines)


def title(payload: dict, suffix: str) -> str:
    """Return an embed title naming the game."""
    return f'{payload["game_name"]} — {suffix}'


def announce_embed(payload: dict, host_mention: str) -> discord.Embed:
    """Return the announcement of a new game."""
    embed = discord.Embed(
        title=payload['game_name'],
        description=f'Hosted by {host_mention} · answer with the button below '
                    f'each round.',
        colour=RUNNING, timestamp=payload.get('created_at'))
    embed.add_field(name='Quiz type', value=payload['type_label'])
    embed.add_field(name='Scoring', value=payload['scoring_label'])
    embed.add_field(name='Questions queued', value=str(payload['queued']))
    return embed


def round_embed(payload: dict) -> discord.Embed:
    """Return the embed of the round in play."""
    embed = discord.Embed(title=title(payload, f'Round {payload["index"]}'),
                          description=f'**{payload["prompt"]}**', colour=RUNNING)
    embed.add_field(name=f'{icon(payload["type"])} {payload["type_label"]}',
                    value=f'{payload["queued"]} question(s) queued')
    embed.set_footer(text=f'Round {payload["index"]} · '
                          f'{payload["scoring_label"]} · answer with the button '
                          f'below')
    return embed


def option_line(option: dict, expected: str) -> str:
    """Return one multiple choice option, marked when it is the right one."""
    return f'{"✅" if option["label"] == expected else "❌"} {option["label"]}'


def media_line(url: str) -> str:
    """Return the hyperlink listening to the track of a question."""
    return f'[🎧 Listen]({url})' if url else ''


def reveal_embed(payload: dict) -> discord.Embed:
    """Return the embed giving the answer of a revealed round."""
    lines = []
    if payload['prompt_set']:
        lines.append(f'**{payload["prompt"]}**')
    lines.append(f'## {payload["answer_text"]}')
    if payload['type'] == QuizType.MULTIPLE_CHOICE and payload['options']:
        lines.append(' · '.join(option_line(option, payload['expected'])
                                for option in payload['options']))
    if payload['media_url']:
        lines.append(media_line(payload['media_url']))
    embed = discord.Embed(
        title=title(payload, f'Round {payload["index"]} answer'),
        description='\n'.join(lines),
        colour=SCORED if payload['right'] else REVEALED)
    embed.add_field(name='Correct answers',
                    value=f'{payload["right"]} of {payload["answered"]}')
    names = payload['right_names']
    if names:
        left = payload['right'] - len(names)
        note = f' …and {left} more.' if left else ''
        embed.add_field(name='Correct players',
                        value=clip(', '.join(names) + note, FIELD_VALUE_LIMIT),
                        inline=False)
    embed.set_footer(text=f'{payload["game_name"]} · round {payload["index"]}')
    return embed


def leader_line(scores: list[dict], verb: str) -> str:
    """Return the callout naming the best player of a scoreboard."""
    if not scores:
        return 'No answer was given.'
    return f'🥇 {row_label(scores[0])} {verb}!'


def scores_embed(payload: dict, suffix: str, lead: str,
                 colour: discord.Colour = REVEALED,
                 fields: Iterable[tuple] = ()) -> discord.Embed:
    """Return a scoreboard: a leader callout, the standings and a footer."""
    embed = discord.Embed(title=title(payload, suffix), description=lead,
                          colour=colour, timestamp=payload.get('finished_at'))
    lines, left = score_lines(payload['scores'][:MAX_LISTED_PLAYERS],
                              FIELD_VALUE_LIMIT - RESERVE)
    if lines:
        embed.add_field(name='Standings', value=block(lines, left), inline=False)
    for name, value, inline in fields:
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text=f'{payload["game_name"]} · {payload["scoring_label"]}')
    return embed


def recap_embed(payload: dict) -> discord.Embed:
    """Return the final scores and the summary of a finished game."""
    fields = []
    if payload['teams']:
        team_lines, teams_left = score_lines(payload['teams'],
                                             FIELD_VALUE_LIMIT - RESERVE)
        fields.append(('Teams', block(team_lines, teams_left, 'teams'), False))
    fields += [('Rounds played', str(payload['rounds']), True),
               ('Answers given', str(payload['answers']), True),
               ('Quiz type', payload['type_label'], True)]
    return scores_embed(payload, 'final scores',
                        leader_line(payload['scores'], 'wins'),
                        colour=FINISHED, fields=fields)
