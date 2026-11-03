"""Every replica follows the sites' operating_mode together (WT-2201).

A PortaSwitch session is site-local, and the secondary-site session sync
(BA-47622) resolves a session used on both sites by last-write-wins, so a refresh
token minted on one site can be overwritten by the other site's older row. Pods
that switched on their own request errors put one session on both sites. When the
sites report operating_mode (BA-47641), a monitor polls both on wall-clock aligned
ticks and is the only thing that switches; requests go to the active site only.
Sites without operating_mode keep the reactive behaviour.
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
from bss.adapters.portaswitch.api.admin import AdminAPI  # noqa: E402
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
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
from report_error import WebTritErrorException, raise_webtrit_error  # noqa: E402

MAIN = "https://main.example.com"
STANDBY = "https://standby.example.com"


class Connector(AsyncHTTPAPIConnector):
    def __init__(self, site_state):
        super().__init__(MAIN, site_state=site_state, standby_server=STANDBY)


class Sites:
    """Scripted operating_mode answers of both sites; None means no answer."""

    def __init__(self, main=OPERATING_MODE_NORMAL, standby=OPERATING_MODE_SECONDARY):
        self.main = main
        self.standby = standby

    async def probe_main(self):
        return self.main

    async def probe_standby(self):
        return self.standby


def monitored(sites, **kwargs):
    state = SiteState(**kwargs)
    state.set_probe(sites.probe_main)
    state.set_standby_probe(sites.probe_standby)
    return state


async def ticks(state, n=1):
    for _ in range(n):
        await state.poll_once()


def stop_monitor(state):
    if state._monitor_task is not None:
        state._monitor_task.cancel()


# --- which mode the tracker is in ----------------------------------------------


@pytest.mark.asyncio
async def test_reactive_until_a_site_reports_a_mode():
    state = monitored(Sites(main=OPERATING_MODE_UNKNOWN, standby=OPERATING_MODE_UNKNOWN))

    assert state.next_targets(MAIN, STANDBY) == [MAIN, STANDBY]
    await ticks(state)

    assert not state.mode_aware
    assert state.next_targets(MAIN, STANDBY) == [MAIN, STANDBY]
    stop_monitor(state)


@pytest.mark.asyncio
async def test_mode_aware_once_the_sites_report_a_mode():
    state = monitored(Sites())

    await ticks(state)

    assert state.mode_aware
    assert state.next_targets(MAIN, STANDBY) == [MAIN]
    stop_monitor(state)


@pytest.mark.asyncio
async def test_no_answer_from_either_site_keeps_the_mode():
    sites = Sites()
    state = monitored(sites)
    await ticks(state)

    sites.main = sites.standby = None
    await ticks(state, 5)

    assert state.mode_aware
    assert state.active is Site.MAIN


@pytest.mark.asyncio
async def test_back_to_reactive_when_the_sites_stop_reporting_a_mode():
    sites = Sites()
    state = monitored(sites)
    await ticks(state)

    sites.main = sites.standby = OPERATING_MODE_UNKNOWN
    await ticks(state)

    assert not state.mode_aware


# --- switching on what the sites report ----------------------------------------


@pytest.mark.asyncio
async def test_standalone_secondary_switches_at_once():
    sites = Sites()
    state = monitored(sites)
    await ticks(state)

    sites.standby = OPERATING_MODE_STANDALONE
    await ticks(state)

    assert state.active is Site.STANDBY


@pytest.mark.asyncio
@pytest.mark.parametrize("main_down", [None, OPERATING_MODE_SECONDARY])
async def test_main_down_switches_only_after_the_threshold(main_down):
    sites = Sites()
    state = monitored(sites, main_down_threshold=3)
    await ticks(state)

    sites.main = main_down
    await ticks(state, 2)
    assert state.active is Site.MAIN

    await ticks(state)
    assert state.active is Site.STANDBY


@pytest.mark.asyncio
async def test_main_down_streak_resets_when_the_main_site_answers():
    sites = Sites()
    state = monitored(sites, main_down_threshold=3)
    await ticks(state)

    sites.main = None
    await ticks(state, 2)
    sites.main = OPERATING_MODE_NORMAL
    await ticks(state)
    sites.main = None
    await ticks(state, 2)

    assert state.active is Site.MAIN


@pytest.mark.asyncio
async def test_main_down_is_not_counted_while_the_secondary_site_is_silent_too():
    sites = Sites()
    state = monitored(sites, main_down_threshold=2)
    await ticks(state)

    sites.main = sites.standby = None
    await ticks(state, 5)

    assert state.active is Site.MAIN


@pytest.mark.asyncio
@pytest.mark.parametrize("main_mode", [OPERATING_MODE_READ_ONLY, OPERATING_MODE_NORMAL])
async def test_main_site_that_answers_is_kept(main_mode):
    sites = Sites(main=main_mode)
    state = monitored(sites, main_down_threshold=1)

    await ticks(state, 3)

    assert state.active is Site.MAIN


@pytest.mark.asyncio
async def test_switch_back_needs_normal_main_and_a_secondary_no_longer_standalone():
    sites = Sites(standby=OPERATING_MODE_STANDALONE)
    state = monitored(sites, switch_back_threshold=2)
    await ticks(state)
    assert state.active is Site.STANDBY

    # The main site is back, but the installation still runs on the secondary.
    await ticks(state, 3)
    assert state.active is Site.STANDBY

    sites.standby = OPERATING_MODE_SECONDARY
    await ticks(state)
    assert state.active is Site.STANDBY
    await ticks(state)
    assert state.active is Site.MAIN


@pytest.mark.asyncio
async def test_switch_back_streak_resets_on_a_main_site_not_normal():
    sites = Sites(main=None)
    state = monitored(sites, main_down_threshold=1, switch_back_threshold=2)
    await ticks(state, 2)
    assert state.active is Site.STANDBY

    sites.main = OPERATING_MODE_NORMAL
    await ticks(state)
    sites.main = OPERATING_MODE_READ_ONLY
    await ticks(state)
    sites.main = OPERATING_MODE_NORMAL
    await ticks(state)

    assert state.active is Site.STANDBY


@pytest.mark.asyncio
async def test_a_probe_that_hangs_counts_as_no_answer():
    async def hangs():
        await asyncio.sleep(3600)

    sites = Sites()
    state = monitored(sites, poll_interval=1, main_down_threshold=1)
    state.set_probe(hangs)

    await ticks(state)

    assert state.mode_aware
    assert state.active is Site.STANDBY


# --- requests while mode-aware --------------------------------------------------


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


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_connect_failure_neither_retries_on_standby_nor_switches(served, error):
    log, main_error = served
    state = monitored(Sites())
    await ticks(state)
    main_error["error"] = error

    with pytest.raises(WebTritErrorException):
        await Connector(state)._send_rest_request("POST", "/rest/Session/ping")

    assert log == ["main.example.com"]
    assert state.active is Site.MAIN
    stop_monitor(state)


@pytest.mark.asyncio
async def test_read_timeout_starts_no_check_of_its_own(served):
    _, main_error = served
    state = monitored(Sites())
    await ticks(state)
    main_error["error"] = httpx.ReadTimeout

    with pytest.raises(WebTritErrorException):
        await Connector(state)._send_rest_request("POST", "/rest/Account/get_xdr_list")

    assert state._confirm_task is None
    assert state.active is Site.MAIN
    stop_monitor(state)


@pytest.mark.asyncio
async def test_requests_follow_the_active_site_only(served):
    log, _ = served
    sites = Sites(standby=OPERATING_MODE_STANDALONE)
    state = monitored(sites)
    await ticks(state)

    await Connector(state)._send_rest_request("POST", "/rest/Session/ping")

    assert log == ["standby.example.com"]
    stop_monitor(state)


@pytest.mark.asyncio
async def test_connect_failure_still_fails_over_at_once_while_reactive(served):
    log, main_error = served
    state = monitored(Sites(main=OPERATING_MODE_UNKNOWN, standby=OPERATING_MODE_UNKNOWN))
    await ticks(state)
    main_error["error"] = httpx.ConnectError

    result = await Connector(state)._send_rest_request("POST", "/rest/Session/ping")

    assert result == {"ok": True}
    assert log == ["main.example.com", "standby.example.com"]
    assert state.active is Site.STANDBY
    stop_monitor(state)


# --- the monitor task -----------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_starts_on_the_first_request_only_with_both_probes():
    state = SiteState()
    state.set_probe(Sites().probe_main)
    state.next_targets(MAIN, STANDBY)
    assert state._monitor_task is None

    state.set_standby_probe(Sites().probe_standby)
    state.next_targets(MAIN, STANDBY)
    task = state._monitor_task
    assert task is not None and not task.done()

    state.next_targets(MAIN, STANDBY)
    assert state._monitor_task is task
    stop_monitor(state)


def test_monitor_is_not_started_without_a_running_loop():
    state = monitored(Sites())

    assert state.next_targets(MAIN, STANDBY) == [MAIN, STANDBY]
    assert state._monitor_task is None


# --- the operating_mode probe keeps its session ---------------------------------


@pytest.fixture
def admin_api(monkeypatch):
    calls = []
    script = {"session_data": [{"operating_mode": OPERATING_MODE_NORMAL}]}

    async def fake_send(self, method, path, server=None, json=None, auth_session=None, **kwargs):
        calls.append((path, server, auth_session.access_token if auth_session else None))
        if path == "/rest/Session/login":
            return {"access_token": f"token-{sum(1 for c in calls if c[0] == path)}"}
        answer = script["session_data"].pop(0) if len(script["session_data"]) > 1 else script["session_data"][0]
        if answer == "unreachable":
            raise_webtrit_error(
                500, bss_response_trace={"status_code": 408, "text": "Timed out", "response_content": {}}
            )
        if isinstance(answer, str):
            raise_webtrit_error(
                500, bss_response_trace={"status_code": 500, "text": "", "response_content": {"faultcode": answer}}
            )
        return answer

    monkeypatch.setattr(AsyncHTTPAPIConnector, "send_rest_request", fake_send)
    api = AdminAPI(PortaSwitchSettings())
    return api, calls, script


@pytest.mark.asyncio
async def test_probe_logs_in_once_per_site_and_reuses_the_session(admin_api):
    api, calls, _ = admin_api

    assert await api.get_operating_mode(MAIN) == OPERATING_MODE_NORMAL
    assert await api.get_operating_mode(MAIN) == OPERATING_MODE_NORMAL
    assert await api.get_operating_mode(STANDBY) == OPERATING_MODE_NORMAL

    logins = [c for c in calls if c[0] == "/rest/Session/login"]
    assert [c[1] for c in logins] == [MAIN, STANDBY]


@pytest.mark.asyncio
async def test_probe_logs_in_again_when_the_site_rejects_the_kept_session(admin_api):
    api, calls, script = admin_api
    await api.get_operating_mode(MAIN)
    script["session_data"] = [
        "Server.Session.check_auth.auth_failed",
        {"operating_mode": OPERATING_MODE_SECONDARY},
    ]

    assert await api.get_operating_mode(MAIN) == OPERATING_MODE_SECONDARY

    data_calls = [c for c in calls if c[0] == "/rest/Generic/get_session_data"]
    assert [c[2] for c in data_calls] == ["token-1", "token-1", "token-2"]


@pytest.mark.asyncio
async def test_probe_of_an_unreachable_site_answers_none_and_keeps_the_session(admin_api):
    api, calls, script = admin_api
    await api.get_operating_mode(MAIN)
    script["session_data"] = ["unreachable", {"operating_mode": OPERATING_MODE_NORMAL}]

    assert await api.get_operating_mode(MAIN) is None
    assert await api.get_operating_mode(MAIN) == OPERATING_MODE_NORMAL

    logins = [c for c in calls if c[0] == "/rest/Session/login"]
    assert len(logins) == 1
