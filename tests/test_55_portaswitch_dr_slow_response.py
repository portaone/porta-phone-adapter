"""A slow main-site response is not a DR outage (WT-2201).

A heavy call such as get_xdr_list can outlast the read timeout on a main site
that answers everything else. That timeout used to switch the whole pod to the
standby, which rejects most requests in secondary mode. Now only a failure to
connect switches at once; a read timeout fails the one request and has the
main site probed out-of-band, and the pod switches only if the probe confirms
an outage.
"""

import asyncio
import os
import sys

import httpx
import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")

import bss.async_http_api as async_http_api  # noqa: E402
from bss.adapters.portaswitch.failover import (  # noqa: E402
    OPERATING_MODE_NORMAL,
    OPERATING_MODE_READ_ONLY,
    OPERATING_MODE_SECONDARY,
    OPERATING_MODE_STANDALONE,
    OPERATING_MODE_UNKNOWN,
    Site,
    SiteState,
)
from bss.async_http_api import AsyncHTTPAPIConnector  # noqa: E402
from report_error import WebTritErrorException  # noqa: E402

MAIN = "https://main.example.com"
STANDBY = "https://standby.example.com"


class Connector(AsyncHTTPAPIConnector):
    def __init__(self, site_state):
        super().__init__(MAIN, site_state=site_state, standby_server=STANDBY)


def probe_returning(mode):
    calls = []

    async def probe():
        calls.append(mode)
        if isinstance(mode, Exception):
            raise mode
        return mode

    return probe, calls


async def settle(state):
    if state._confirm_task is not None:
        await state._confirm_task


@pytest.fixture
def served(monkeypatch):
    """Route requests through a mock transport whose main-site behaviour a test picks."""
    log = []
    main_error = {}

    def handler(request):
        log.append(request.url.host)
        if request.url.host == "main.example.com" and "error" in main_error:
            raise main_error["error"]("main site", request=request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_shared_client(verify, limits=None):
        return client

    monkeypatch.setattr(async_http_api, "get_shared_async_client", fake_shared_client)
    return log, main_error


# --- the request layer --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout])
async def test_read_timeout_is_not_retried_on_standby_and_does_not_switch(served, error):
    log, main_error = served
    main_error["error"] = error
    probe, calls = probe_returning(OPERATING_MODE_NORMAL)
    state = SiteState()
    state.set_probe(probe)

    with pytest.raises(WebTritErrorException):
        await Connector(state)._send_rest_request("POST", "/rest/Account/get_xdr_list")
    await settle(state)

    assert log == ["main.example.com"]
    assert calls == [OPERATING_MODE_NORMAL]
    assert state.active is Site.MAIN


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_connect_failure_still_fails_over_at_once(served, error):
    log, main_error = served
    main_error["error"] = error
    probe, calls = probe_returning(OPERATING_MODE_NORMAL)
    state = SiteState()
    state.set_probe(probe)

    result = await Connector(state)._send_rest_request("POST", "/rest/Session/ping")

    assert result == {"ok": True}
    assert log == ["main.example.com", "standby.example.com"]
    assert calls == []
    assert state.active is Site.STANDBY


@pytest.mark.asyncio
async def test_pool_timeout_neither_switches_nor_probes(served):
    _, main_error = served
    main_error["error"] = httpx.PoolTimeout
    probe, calls = probe_returning(None)
    state = SiteState()
    state.set_probe(probe)

    with pytest.raises(WebTritErrorException):
        await Connector(state)._send_rest_request("POST", "/rest/Session/ping")
    await settle(state)

    assert calls == []
    assert state.active is Site.MAIN


# --- the out-of-band check ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected", [
    (OPERATING_MODE_NORMAL, Site.MAIN),
    (OPERATING_MODE_UNKNOWN, Site.MAIN),
    (OPERATING_MODE_READ_ONLY, Site.MAIN),
    (OPERATING_MODE_STANDALONE, Site.MAIN),
    (OPERATING_MODE_SECONDARY, Site.STANDBY),
    (None, Site.STANDBY),
    (RuntimeError("boom"), Site.STANDBY),
])
async def test_slow_main_switches_only_on_a_confirmed_outage(mode, expected):
    probe, _ = probe_returning(mode)
    state = SiteState()
    state.set_probe(probe)

    state.report_slow("/rest/Account/get_xdr_list")
    await settle(state)

    assert state.active is expected


@pytest.mark.asyncio
async def test_concurrent_slow_responses_share_one_check():
    release = asyncio.Event()
    calls = []

    async def probe():
        calls.append(1)
        await release.wait()
        return OPERATING_MODE_NORMAL

    state = SiteState()
    state.set_probe(probe)

    for _ in range(5):
        state.report_slow("/rest/Account/get_xdr_list")
    await asyncio.sleep(0)
    release.set()
    await settle(state)

    assert calls == [1]
    state.report_slow("/rest/Account/get_xdr_list")
    await settle(state)
    assert calls == [1, 1]


@pytest.mark.asyncio
async def test_slow_response_on_standby_is_ignored():
    probe, calls = probe_returning(None)
    state = SiteState()
    state.set_probe(probe)
    state.report_unreachable()

    state.report_slow("/rest/Account/get_xdr_list")
    await settle(state)

    assert calls == []
    assert state.active is Site.STANDBY


def test_without_a_probe_a_slow_response_switches_as_before():
    state = SiteState()
    state.report_slow("/rest/Account/get_xdr_list")
    assert state.active is Site.STANDBY
