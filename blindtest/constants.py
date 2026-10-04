"""Game rules, defaults and limits every layer of the app reads."""

from django.utils.translation import gettext_lazy as _

POINTS_PER_TEXT = 1
POINTS_PER_SECONDARY = 1
# Extra points for the three fastest correct answers: 3, 2 and 1 in total.
SPEED_BONUS = (2, 1, 0)
# Shown by a blind test round whose question defines no prompt.
DEFAULT_BLIND_TEST_PROMPT = _('Listen to the music and guess the title '
                              'and/or the artist!')
# Highest year a question may carry.
MAX_YEAR = 9999
# Fields of a question a host edits from Discord, in question add order.
EDITABLE_FIELDS = ('answer', 'artist', 'prompt', 'choices', 'year', 'album',
                   'media')
# Value a host gives one of those fields to drop it.
CLEAR_VALUE = '-'
# A Discord select menu offers at most 25 options.
MAX_CHOICES = 25
# How many players an embed lists before it says "and more".
MAX_LISTED_PLAYERS = 25
# A Discord option label and an autocomplete choice name hold 100 characters.
CHOICE_NAME_LIMIT = 100
# Said whenever a host reaches for a game that is already over.
QUIZ_OVER = _('This quiz is over.')
# How long a client may hold a broadcast before another may take it over.
BROADCAST_CLAIM_TIMEOUT = 300
# How many broadcasts a client posts in one pass.
BROADCAST_BATCH = 20
# How much of a failure reason a broadcast keeps.
BROADCAST_ERROR_LIMIT = 500

