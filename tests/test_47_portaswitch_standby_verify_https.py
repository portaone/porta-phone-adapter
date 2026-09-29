"""TLS verification of the standby site is configured on its own (WT-2035).

A disaster-recovery standby is usually reached by IP, while its certificate
names the main site's domain - which resolves to the main site, so it cannot go
into the standby URL. With one verify setting for both sites, verification had
to be off everywhere for failover to work. PORTASWITCH_VERIFY_HTTPS_STANDBY
decides the standby alone, and unset it follows PORTASWITCH_VERIFY_HTTPS.
"""

import os
import sys

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

import bss.async_http_api as async_http_api  # noqa: E402
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
from bss.adapters.portaswitch.failover import SiteState  # noqa: E402
from bss.adapters.portaswitch.api.account import AccountAPI  # noqa: E402
from bss.adapters.portaswitch.api.admin import AdminAPI  # noqa: E402
from bss.async_http_api import AsyncHTTPAPIConnector  # noqa: E402

MAIN = "https://main.example.com"
STANDBY = "https://203.0.113.10:1443"

_REQUIRED = {
    "ADMIN_API_URL": MAIN + "/admin",
    "ADMIN_API_LOGIN": "admin",
    "ADMIN_API_TOKEN": "token",
    "ACCOUNT_API_URL": MAIN + "/account",
}


def settings(**overrides) -> PortaSwitchSettings:
    return PortaSwitchSettings(**_REQUIRED, **overrides)


# --- what the setting resolves to -------------------------------------------


@pytest.mark.parametrize("unset", [None, "", "   "])
def test_unset_standby_verify_follows_the_main_setting(unset):
    assert settings(VERIFY_HTTPS_STANDBY=unset).VERIFY_HTTPS_STANDBY is None


@pytest.mark.parametrize("value, expected", [("false", False), ("true", True), (False, False)])
def test_an_explicit_standby_verify_is_honoured(value, expected):
    assert settings(VERIFY_HTTPS_STANDBY=value).VERIFY_HTTPS_STANDBY is expected


# --- which verify each target gets ------------------------------------------


class Connector(AsyncHTTPAPIConnector):
    def __init__(self, verify_main, verify_standby, standby_server=STANDBY):
        super().__init__(MAIN, site_state=SiteState(), standby_server=standby_server)
        self._verify_https = verify_main
        self._verify_https_standby = verify_standby


@pytest.mark.parametrize("verify_main, verify_standby, expected_main, expected_standby", [
    (True, None, True, True),     # unset: the standby follows the main site
    (False, None, False, False),
    (True, False, True, False),   # the WT-2035 setup: only the standby is not verified
    (False, True, False, True),
])
def test_each_site_gets_its_own_verify(verify_main, verify_standby, expected_main, expected_standby):
    connector = Connector(verify_main, verify_standby)
    assert connector._verify_for(MAIN) is expected_main
    assert connector._verify_for(STANDBY) is expected_standby


def test_without_failover_the_standby_setting_is_ignored():
    connector = Connector(True, False, standby_server=None)
    assert connector._verify_for(MAIN) is True
    assert connector._verify_for(STANDBY) is True


@pytest.mark.parametrize("api_class, standby_field", [
    (AdminAPI, "ADMIN_API_URL_STANDBY"),
    (AccountAPI, "ACCOUNT_API_URL_STANDBY"),
])
def test_both_realms_take_the_standby_setting(api_class, standby_field):
    api = api_class(settings(VERIFY_HTTPS_STANDBY="false", **{standby_field: STANDBY}), site_state=SiteState())
    assert api._verify_for(api.api_server) is True
    assert api._verify_for(STANDBY) is False


# --- the failover request actually uses the standby's client ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_failover_request_goes_through_the_standby_client(monkeypatch, stream):
    """Main unreachable -> the retry against the standby uses the verify=False client."""
    served = []

    def handler_for(verify):
        def handler(request):
            served.append((verify, request.url.host))
            if request.url.host == "main.example.com":
                raise httpx.ConnectError("main site down", request=request)
            return httpx.Response(200, json={"ok": True})
        return handler

    clients = {v: httpx.AsyncClient(transport=httpx.MockTransport(handler_for(v))) for v in (True, False)}

    async def fake_shared_client(verify, limits=None):
        return clients[verify]

    monkeypatch.setattr(async_http_api, "get_shared_async_client", fake_shared_client)
    connector = Connector(verify_main=True, verify_standby=False)
    try:
        result = await connector._send_rest_request("POST", "/rest/Session/ping", stream=stream)
    finally:
        for client in clients.values():
            await client.aclose()

    assert result == {"ok": True}
    assert served == [(True, "main.example.com"), (False, "203.0.113.10")]
