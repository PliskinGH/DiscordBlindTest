"""The outbox: the public posts a game owes, delivered by whichever client holds
the Discord connection.
"""


from collections.abc import Iterable
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _ 
from discordcore.members import DiscordMember
from discordcore.models import Guild

from ..constants import (BROADCAST_BATCH, BROADCAST_CLAIM_TIMEOUT,
                         BROADCAST_ERROR_LIMIT, BROADCAST_MAX_ATTEMPTS,
                         BROADCAST_PANEL_SIZE, BROADCAST_RETRY_BASE_SECONDS,
                         BROADCAST_RETRY_MAX_SECONDS)
from ..models import Broadcast, Game, Question, Round
from .games import (publication_payload, end_game, publish_game,
                    recap_payload)
from .rounds import reveal_payload, reveal_round, round_payload, open_round


def enqueue(game: Game, kind: str, *, round_: Round | None = None,
            claim: bool = False) -> Broadcast:
    """Record a public post a game still owes.
    ``claim`` marks the row as owned by the caller enqueueing it. The bot does
    so in the same transaction as the transition, which keeps the worker from
    taking over a post that is already being made.
    """
    return Broadcast.objects.create(
        game=game, kind=kind, round=round_,
        status=Broadcast.Status.CLAIMED if claim else Broadcast.Status.PENDING,
        claimed_at=timezone.now() if claim else None)


def _claimable() -> Q:
    """Return the broadcasts a client may take: free, stale, or due a retry."""
    stale = timezone.now() - timedelta(seconds=BROADCAST_CLAIM_TIMEOUT)
    return (Q(status=Broadcast.Status.PENDING, claimed_at__isnull=True)
            | Q(status=Broadcast.Status.CLAIMED, claimed_at__lt=stale)
            | Q(status=Broadcast.Status.FAILED,
                next_attempt_at__lte=timezone.now()))


def unfinished_broadcasts(guild: Guild,
                         limit: int = BROADCAST_PANEL_SIZE) -> list[dict]:
    """Return the posts of one guild that are not posted yet, newest first.
    Rows come back as dictionaries because the panel shows a status, a count and
    a reason rather than the whole row. Read uncached: a stale row would hide a
    post that is waiting for a hand.
    """
    rows = (Broadcast.objects
            .filter(game__guild=guild)
            .exclude(status=Broadcast.Status.SENT)
            .select_related('game__guild')
            .order_by('-created_at', '-pk')[:limit])
    return [_broadcast_row(row) for row in rows]


def game_broadcasts(game: Game,
                   limit: int = BROADCAST_PANEL_SIZE) -> list[dict]:
    """Return the recent posts of one game, of any status, newest first.
    The control room shows what went out as well as what is stuck, so a host can
    confirm a post without leaving the page. Read uncached like the guild panel:
    a stale row would misreport a post that is still waiting for a hand.
    """
    rows = (Broadcast.objects
            .filter(game=game)
            .select_related('game')
            .order_by('-created_at', '-pk')[:limit])
    return [_broadcast_row(row) for row in rows]


def _broadcast_row(row: Broadcast) -> dict:
    """Return what a broadcast panel shows of one post."""
    return {'broadcast': row.pk,
            'game': row.game,
            'kind': row.get_kind_display(),
            'status': row.get_status_display(),
            'dead': row.status == Broadcast.Status.DEAD,
            'attempts': row.attempts,
            'next_attempt_at': row.next_attempt_at,
            'error': (row.error or '')[:BROADCAST_ERROR_LIMIT]}


def retry_broadcast(broadcast_pk: int, guild_id: int) -> Broadcast:
    """Put a post back in line, and return it.
    The post must belong to the named guild row. Raises
    ``Broadcast.DoesNotExist`` when no such post was recorded for that server.
    """
    return reset_broadcast(Broadcast.objects.get(pk=broadcast_pk,
                                                game__guild_id=guild_id))


def retry_delay(attempts: int) -> int:
    """Return how long to wait before the attempt after the one that failed."""
    return min(BROADCAST_RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0),
               BROADCAST_RETRY_MAX_SECONDS)


def claim_broadcast(broadcast_pk: int) -> bool:
    """Take a broadcast for delivery, refusing one already taken or sent.
    Read uncached: the claim is what keeps two clients off one post, and a cached
    answer would let both take it.
    """
    return bool(Broadcast.objects.filter(pk=broadcast_pk)
                .filter(_claimable())
                .update(status=Broadcast.Status.CLAIMED,
                        claimed_at=timezone.now()))


def pending_broadcasts(limit: int = BROADCAST_BATCH) -> list[Broadcast]:
    """Return the broadcasts the client owes, oldest first.
    Read uncached, for the same reason as ``claim_broadcast``.
    """
    return list((Broadcast.objects
                 .filter(_claimable())
                 .select_related('game__host', 'round__game')
                 .order_by('created_at', 'pk')[:limit]))


def broadcast_payload(broadcast: Broadcast) -> dict:
    """Rebuild what a broadcast shows, from the state it was recorded for.
    Built uncached: this is the message the server reads, and a stale one would
    post the wrong scores.
    """
    if broadcast.kind == Broadcast.Kind.PUBLISH:
        return publication_payload(broadcast.game)
    if broadcast.kind == Broadcast.Kind.RECAP:
        return recap_payload(broadcast.game)
    if broadcast.round_id is None:
        raise ValueError(_("This post has no round to publish."))
    if broadcast.kind == Broadcast.Kind.ROUND:
        return round_payload(broadcast.round)
    return reveal_payload(broadcast.round)


def mark_broadcast_sent(broadcast: Broadcast,
                        message_ids: Iterable[int] = ()) -> Broadcast:
    """Record the messages a broadcast produced."""
    broadcast.status = Broadcast.Status.SENT
    broadcast.sent_at = timezone.now()
    broadcast.message_ids = [int(pk) for pk in message_ids]
    broadcast.save(update_fields=['status', 'sent_at', 'message_ids'])
    return broadcast


def mark_broadcast_failed(broadcast: Broadcast, reason: str) -> Broadcast:
    """Record a failed attempt and schedule the next one, or give the post up."""
    broadcast.attempts += 1
    broadcast.error = str(reason)[:BROADCAST_ERROR_LIMIT]
    if broadcast.attempts >= BROADCAST_MAX_ATTEMPTS:
        broadcast.status = Broadcast.Status.DEAD
        broadcast.next_attempt_at = None
    else:
        broadcast.status = Broadcast.Status.FAILED
        broadcast.next_attempt_at = (
            timezone.now()
            + timedelta(seconds=retry_delay(broadcast.attempts)))
    broadcast.save(update_fields=['status', 'attempts', 'next_attempt_at',
                                  'error'])
    return broadcast


def reset_broadcast(broadcast: Broadcast) -> Broadcast:
    """Put a post back in line for a fresh try, as if it had just been enqueued."""
    broadcast.status = Broadcast.Status.PENDING
    broadcast.claimed_at = None
    broadcast.sent_at = None
    broadcast.next_attempt_at = None
    broadcast.attempts = 0
    broadcast.save(update_fields=['status', 'claimed_at', 'sent_at',
                                  'next_attempt_at', 'attempts'])
    return broadcast


@transaction.atomic
def post_publication(game: Game, host_member: DiscordMember, *,
                     claim: bool = False) -> tuple[dict, Broadcast]:
    """Publish a game and record the publication its client must post."""
    payload = publish_game(game, host_member)
    return payload, enqueue(game, Broadcast.Kind.PUBLISH, claim=claim)


@transaction.atomic
def post_round_open(game: Game, host_member: DiscordMember,
                    question: Question | None = None, quiz_type: str = '',
                    *, claim: bool = False) -> tuple[dict, Broadcast]:
    """Open the next round and record the message its client must post."""
    payload = open_round(game, host_member, question, quiz_type)
    return payload, enqueue(game, Broadcast.Kind.ROUND,
                            round_=Round.objects.get(pk=payload['round_id']),
                            claim=claim)


@transaction.atomic
def post_round_reveal(round_: Round, host_member: DiscordMember, *,
                      claim: bool = False) -> tuple[dict, Broadcast]:
    """Reveal a round and record the posts its client must make."""
    payload = reveal_round(round_, host_member)
    return payload, enqueue(round_.game, Broadcast.Kind.REVEAL, round_=round_,
                            claim=claim)


@transaction.atomic
def post_game_end(game: Game, host_member: DiscordMember, *,
                  claim: bool = False
                  ) -> tuple[dict, list[tuple[Broadcast, dict]]]:
    """End a game, recording the reveal of its open round and the recap.
    A game that ends with a round still open owes two posts: the answer of that
    round, and the final scores. Splitting them keeps every broadcast tied to one
    message, and each is returned with the payload its own message is built from,
    so a client posting them now and one posting them later render the same.
    """
    payload = end_game(game, host_member)
    reveal = payload['reveal']
    posts = []
    if reveal is not None:
        posts.append((enqueue(
            game, Broadcast.Kind.REVEAL,
            round_=Round.objects.get(pk=reveal['round_id']), claim=claim), reveal))
    posts.append((enqueue(game, Broadcast.Kind.RECAP, claim=claim),
                  {key: value for key, value in payload.items()
                   if key != 'reveal'}))
    return payload, posts
