"""Points of a round, computed from the answers instead of stored on them."""

from collections.abc import Iterable

from django.utils.translation import gettext as _

from .constants import POINTS_PER_SECONDARY, POINTS_PER_TEXT, SPEED_BONUS
from .models import Guess, Round, ScoringMode


def round_points(round_: Round,
                 guesses: Iterable[Guess] | None = None) -> dict[int, int]:
    """Return the points earned by each guess of the round, keyed by guess id.

    The totals depend on the other answers of the round (first only, speed),
    which is why no points are stored on the guesses themselves. A caller that
    read the guesses already passes them in, in submission order.
    """
    if guesses is None:
        guesses = round_.guesses.order_by('submitted_at', 'pk')
    guesses = list(guesses)
    points = {guess.pk: 0 for guess in guesses}
    mode = round_.effective_scoring_mode
    categories = (('text_correct', POINTS_PER_TEXT),
                  ('secondary_correct', POINTS_PER_SECONDARY))
    for correct_field, base in categories:
        correct = [guess for guess in guesses if getattr(guess, correct_field)]
        if mode == ScoringMode.FIRST_ONLY:
            correct = correct[:1]
        for position, guess in enumerate(correct):
            if mode == ScoringMode.SPEED and position < len(SPEED_BONUS):
                points[guess.pk] += base + SPEED_BONUS[position]
            else:
                points[guess.pk] += base
    return points
