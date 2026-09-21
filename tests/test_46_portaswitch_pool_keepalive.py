"""The keep-alive budget follows the pool size (WT-1973).

The adapter talks to one switch, so every connection it closes after a response
is a TLS handshake it will pay again on the next call. With a pool of 100 and
httpx's own keep-alive default of 20 it paid that on essentially every call:
httpcore charges *active* connections against the keep-alive budget, so once
more than 20 connections were open at all, each one was closed the moment it
went idle. At ~120 new connections per second to a single IP:port the egress NAT
ran out of source ports and calls to PortaBilling started failing (DO-6069).
"""

import os
import sys

import httpcore
import httpx
import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

# PortaSwitchSettings is instantiated at import time and its URL/credential
# fields are mandatory; supply throwaway values before importing the config.
os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")

from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
from bss.async_http_api import AsyncHTTPAPIConnector  # noqa: E402

_REQUIRED = {
    "ADMIN_API_URL": "https://pbx.example.com/admin",
    "ADMIN_API_LOGIN": "admin",
    "ADMIN_API_TOKEN": "token",
    "ACCOUNT_API_URL": "https://pbx.example.com/account",
}


def settings(**overrides) -> PortaSwitchSettings:
    return PortaSwitchSettings(**_REQUIRED, **overrides)


# --- what the setting resolves to -------------------------------------------


@pytest.mark.parametrize("unset", [None, "", "   ", "0", "-5", "nonsense"])
def test_unset_keepalive_means_as_many_as_the_pool_holds(unset):
    """Blank, absent or nonsensical all mean "do not cap idle connections"."""
    assert settings(MAX_KEEPALIVE_CONNECTIONS=unset).MAX_KEEPALIVE_CONNECTIONS is None


def test_an_explicit_keepalive_limit_is_still_honoured():
    """The option stays configurable — a deployment may cap idle sockets."""
    assert settings(MAX_KEEPALIVE_CONNECTIONS="30").MAX_KEEPALIVE_CONNECTIONS == 30


def test_the_pool_size_is_unchanged():
    assert settings().MAX_CONNECTIONS == 100
    assert settings(MAX_CONNECTIONS="40").MAX_CONNECTIONS == 40


# --- what the connector hands to httpx --------------------------------------


class Connector(AsyncHTTPAPIConnector):
    """A bare connector carrying the pool limits, as AdminAPI/AccountAPI do."""

    def __init__(self, conf: PortaSwitchSettings):
        super().__init__(conf.ADMIN_API_URL)
        self._max_connections = conf.MAX_CONNECTIONS
        self._max_keepalive_connections = conf.MAX_KEEPALIVE_CONNECTIONS

    async def _send_rest_request(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("no request is sent in this test")


def pool_limits(limits: httpx.Limits):
    """The two numbers the real pool ends up with, via httpcore's own clamping."""
    pool = httpcore.AsyncConnectionPool(
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
    )
    return pool._max_connections, pool._max_keepalive_connections


@pytest.mark.parametrize("max_connections", ["100", "40", "7"])
def test_the_keepalive_budget_matches_the_pool_size(max_connections):
    """Whatever the pool size, nothing is kept back from being kept alive.

    httpx maps ``None`` to sys.maxsize and httpcore mins it with max_connections,
    so "unset" and "equal to the pool" are the same pool.
    """
    conf = settings(MAX_CONNECTIONS=max_connections)
    total, keepalive = pool_limits(Connector(conf)._limits())
    assert total == int(max_connections)
    assert keepalive == total


def test_an_explicit_budget_still_reaches_the_pool():
    conf = settings(MAX_CONNECTIONS="100", MAX_KEEPALIVE_CONNECTIONS="30")
    assert pool_limits(Connector(conf)._limits()) == (100, 30)


# --- the behaviour that all of the above exists for -------------------------


class FakeConnection(httpcore.AsyncConnectionInterface):
    """Enough of a connection for the pool's cleanup pass to judge it."""

    def __init__(self, idle: bool):
        self._idle = idle
        self.closed = False

    def is_idle(self) -> bool:
        return self._idle

    def is_closed(self) -> bool:
        return self.closed

    def is_available(self) -> bool:
        return self._idle

    def has_expired(self) -> bool:
        return False

    async def aclose(self) -> None:
        self.closed = True


def surviving_idle_connections(idle: int, active: int, max_connections: int, max_keepalive_connections):
    """Run httpcore's own cleanup pass and report what it left behind.

    ``_assign_requests_to_connections`` is the pass that runs on every response
    close (``PoolByteStream.aclose``), which is where the churn came from.
    """
    pool = httpcore.AsyncConnectionPool(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
    )
    pool._connections = [FakeConnection(idle=True) for _ in range(idle)] + [
        FakeConnection(idle=False) for _ in range(active)
    ]
    pool._assign_requests_to_connections()
    return sum(1 for c in pool._connections if c.is_idle())


def test_a_budget_below_the_pool_drops_idle_connections():
    """What the old 20 cost: idle connections above the budget are closed.

    Asserted on the count alone, which holds however httpcore counts. In
    production it was worse — busy connections are charged against the budget
    too, so with 25 of them in flight even five idle ones were dropped — but
    that arithmetic is upstream's, so pinning it here would only turn red the
    day upstream corrects it.
    """
    assert surviving_idle_connections(idle=30, active=0, max_connections=100, max_keepalive_connections=20) == 20


def test_an_unset_budget_keeps_every_idle_connection():
    """The pool never holds more than max_connections, so nothing is surplus."""
    assert surviving_idle_connections(idle=30, active=0, max_connections=100, max_keepalive_connections=None) == 30
    assert surviving_idle_connections(idle=60, active=40, max_connections=100, max_keepalive_connections=None) == 60
