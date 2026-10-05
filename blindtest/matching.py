"""Comparison of guessed answers with the expected title or artist."""

from collections.abc import Iterable
import re
import unicodedata

NON_WORD_RE = re.compile(r'[\W_]+')


def normalize(text: str | None) -> str:
    """Return a comparable form: lowercase, unaccented, punctuation removed."""
    if not text:
        return ''
    decomposed = unicodedata.normalize('NFKD', str(text).casefold())
    without_accents = ''.join(char for char in decomposed
                              if not unicodedata.combining(char))
    return ' '.join(NON_WORD_RE.sub(' ', without_accents).split())


def answers_match(guess: str | None, answer: str | None) -> bool:
    """Return True when a non-empty guess matches the expected answer."""
    normalized_guess = normalize(guess)
    return bool(normalized_guess) and normalized_guess == normalize(answer)


def matches_any(guess: str | None, canonical: str | None,
                variants: Iterable[str] = ()) -> bool:
    """Return True if guess matches canonical answer or any of its variants."""
    normalized_guess = normalize(guess)
    if not normalized_guess:
        return False
    if canonical and normalized_guess == normalize(canonical):
        return True
    return any(normalized_guess == normalize(variant) for variant in variants)


def matches_normalized(guess: str | None,
                       accepted: Iterable[str]) -> bool:
    """Return True when a guess matches one of already normalized answer forms.

    The forms of an answer are cached in their comparable shape, so a guess is
    normalized once here rather than again for every variant it may match.
    """
    normalized_guess = normalize(guess)
    if not normalized_guess:
        return False
    return normalized_guess in set(accepted)

