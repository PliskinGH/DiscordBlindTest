"""Shared base classes for the test suites.

Every suite runs without the network: a test that forgets to mock a Discord
read fails here, rather than calling the real API or hanging on a socket.
"""

from unittest import mock


class NetworkAccessDenied(AssertionError):
    """A test tried to reach the network, which must never happen."""


def _no_network(*args, **kwargs):
    """Refuse an outgoing request, so a forgotten mock fails the test."""
    raise NetworkAccessDenied(
        'A test made a real HTTP request without mocking it. Patch the '
        'discord_api helper it calls, or the service beneath it.')


class NoNetworkMixin:
    """Block the network for the whole class, whichever base it is mixed into.

    Applied in ``setUpClass`` rather than ``setUp`` so a subclass that forgets
    to call ``super().setUp()`` is still guarded.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        patcher = mock.patch('requests.Session.send', _no_network)
        patcher.start()
        cls.addClassCleanup(patcher.stop)
