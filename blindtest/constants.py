"""Game rules, defaults and limits every layer of the app reads."""

from django.utils.translation import gettext_lazy as _

POINTS_PER_TEXT = 1
POINTS_PER_SECONDARY = 1
# Extra points for the three fastest correct guesses: 3, 2 and 1 in total.
SPEED_BONUS = (2, 1, 0)
# Shown by a blind test round whose question defines no prompt.
DEFAULT_BLIND_TEST_PROMPT = _('Listen to the music and guess the title '
                              'and/or the artist!')
# Highest year a question may carry.
MAX_YEAR = 9999
# Fields of a question a host edits from Discord, in question add order.
EDITABLE_FIELDS = ('answer', 'artist', 'prompt', 'choices', 'year', 'album',
                   'media')
# A Discord select menu offers at most 25 options.
MAX_CHOICES = 25
# How many players an embed lists before it says "and more".
MAX_LISTED_PLAYERS = 25
# A Discord option label and an autocomplete choice name hold 100 characters.
CHOICE_NAME_LIMIT = 100
# Said whenever a host reaches for a game that is already over.
QUIZ_OVER = _('This game is over.')
# The guess form of both front ends, worded once: the Discord modal shows a
# hint as a placeholder, the web admin as help text.
GUESS_ANSWER_LABEL = _('Answer')
GUESS_ANSWER_HINT = _('Song title, or whatever the question asks for.')
GUESS_SECONDARY_LABEL = _('Secondary answer')
GUESS_SECONDARY_HINT = _('Artist for blind tests, or secondary required '
                         'answer.')
# Offered by the modal's pick, which no web admin field mirrors.
GUESS_PICK_PLACEHOLDER = _('Pick your answer')
# How long a client may hold a broadcast before another may take it over.
BROADCAST_CLAIM_TIMEOUT = 300
# How many broadcasts a client posts in one pass.
BROADCAST_BATCH = 20
# How much of a failure reason a broadcast keeps.
BROADCAST_ERROR_LIMIT = 500
# How long to wait before the attempt after a failed one, and the longest wait.
BROADCAST_RETRY_BASE_SECONDS = 30
BROADCAST_RETRY_MAX_SECONDS = 3600
# How many times a post is tried before it is given up on.
BROADCAST_MAX_ATTEMPTS = 6
# How many stuck posts the dashboard lists.
BROADCAST_PANEL_SIZE = 20
# How many questions a library page lists at once.
LIBRARY_PAGE_SIZE = 50

