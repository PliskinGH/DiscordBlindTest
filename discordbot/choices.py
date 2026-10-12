"""How a Discord command option names a picker row, by value or by label.

Discord sends the choice value in the normal case, the displayed label when
the host re-selects. Every parser accepts both, reading the choices through
the same source the autocomplete offers.
"""

from discord import app_commands

from blindtest.constants import CHOICE_NAME_LIMIT


def option_label(choice: dict) -> str:
    """Return the label of a picker option, flagged for media and clipped."""
    prefix = '🎵 ' if choice.get('media') else ''
    return (prefix + choice['label'])[:CHOICE_NAME_LIMIT]


def choice_term(raw: str) -> str:
    """Return a displayed label as a choice search term, decorations undone."""
    return raw.strip().removeprefix('🎵 ').removesuffix('…').strip()


def choice_pk(raw: str, choices: 'list[dict] | None') -> int | None:
    """Return the pk a command option names: by value first, then by label."""
    wanted = str(raw).strip()
    for choice in choices or []:
        if str(choice['pk']) == wanted:
            return int(choice['pk'])
    for choice in choices or []:
        if wanted in (choice['label'], option_label(choice)):
            return int(choice['pk'])
    return None


def as_choices(rows: 'list[dict] | None') -> list[app_commands.Choice[str]]:
    """Return picker options as autocomplete choices."""
    return [app_commands.Choice(name=option_label(choice),
                                value=str(choice['pk']))
            for choice in rows or []]


async def picked_pk(fetch, raw: str) -> int | None:
    """Return the pk an option names, reading the choices through its term.

    ``fetch`` receives a search term and returns the choices. A composed
    label is retried by its head and its tail, since a server side search
    matches the fields the label was built from rather than the label.
    """
    term = choice_term(raw)
    picked = choice_pk(raw, await fetch(term))
    if picked is None and ' — ' in term:
        picked = choice_pk(raw, await fetch(term.split(' — ')[0]))
        if picked is None:
            picked = choice_pk(raw, await fetch(term.rsplit(' — ')[-1]))
    return picked


async def resolve_pk(fetch, raw: str) -> int | None:
    """Return the pk an option names: a digit goes direct, else by label.

    ``fetch`` is only called for a label: a digit needs no choice lookup.
    """
    wanted = str(raw).strip()
    if wanted.isdigit():
        return int(wanted)
    return await picked_pk(fetch, raw)
