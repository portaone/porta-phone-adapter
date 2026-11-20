"""What the adapter keeps when PortaSwitch fails mid-request (WT-2259).

A session refresh that PortaSwitch has already rotated must reach Core even when the
account read after it fails, and an error body that is not JSON must still show up in
the request trace.
"""

import os
import sys
import time
import types

import httpx
import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

import jwt  # noqa: E402

import bss.async_http_api as async_http_api  # noqa: E402
from bss.adapters.portaswitch.adapter import PortaSwitchAdapter  # noqa: E402
from bss.async_http_api import AsyncHTTPAPIConnector  # noqa: E402
from report_error import WebTritErrorException  # noqa: E402

I_ACCOUNT = 102398
I_CUSTOMER = 7001


def token(**claims):
    return jwt.encode({"exp": int(time.time()) + 3600, **claims}, "secret", algorithm="HS256")


class FailingAccountAPI:
    """Refreshes, then fails the account read the way a crashed PortaSwitch does."""

    def __init__(self, access_token, account_read_error=None):
        self._access_token = access_token
        self._error = account_read_error

    async def refresh(self, refresh_token):
        return {"access_token": self._access_token, "refresh_token": "rt-2", "expires_in": 3600}

    async def get_account_info(self, access_token):
        if self._error:
            raise self._error
        return {"account_info": {"i_account": I_ACCOUNT, "i_customer": I_CUSTOMER}}


def adapter(account_api):
    subject = PortaSwitchAdapter.__new__(PortaSwitchAdapter)
    subject._settings = types.SimpleNamespace(ENABLE_ON_DEMAND_SESSION_MIGRATION=False)
    subject._account_api = account_api
    return subject


def server_error():
    return WebTritErrorException(500, "Request execution error on the BSS/VoIP system side")


class TestRefreshSession:
    @pytest.mark.asyncio
    async def test_reads_the_customer_when_it_can(self):
        session = await adapter(FailingAccountAPI(token(i_account=I_ACCOUNT))).refresh_session("rt")

        assert session.user_id.root == str(I_ACCOUNT)
        assert session.customer_id == str(I_CUSTOMER)

    @pytest.mark.asyncio
    async def test_hands_over_the_rotated_tokens_when_the_account_read_fails(self):
        access_token = token(i_account=I_ACCOUNT)
        api = FailingAccountAPI(access_token, account_read_error=server_error())

        session = await adapter(api).refresh_session("rt")

        assert session.user_id.root == str(I_ACCOUNT)
        assert session.access_token.root == access_token
        assert session.refresh_token.root == "rt-2"
        assert session.customer_id is None

    @pytest.mark.asyncio
    async def test_still_fails_for_a_token_that_does_not_name_its_account(self):
        api = FailingAccountAPI(token(), account_read_error=server_error())

        with pytest.raises(WebTritErrorException):
            await adapter(api).refresh_session("rt")


@pytest.fixture
def answers(monkeypatch):
    """The BSS answers every request with the response a test sets."""
    reply = {}

    def handler(request):
        return reply["response"]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_shared_client(verify, limits=None):
        return client

    monkeypatch.setattr(async_http_api, "get_shared_async_client", fake_shared_client)
    return reply


class Connector(AsyncHTTPAPIConnector):
    def __init__(self):
        super().__init__("https://pbx.example.com")


class TestErrorTrace:
    @pytest.mark.asyncio
    async def test_keeps_a_body_that_is_not_json(self, answers):
        answers["response"] = httpx.Response(502, text="Bad Gateway from the proxy")

        with pytest.raises(WebTritErrorException) as raised:
            await Connector()._send_rest_request("POST", "/rest/Session/refresh_access_token")

        trace = raised.value.bss_response_trace
        assert trace["status_code"] == 502
        assert "Bad Gateway from the proxy" in trace["text"]
        assert trace["response_content"] == {}

    @pytest.mark.asyncio
    async def test_a_json_fault_is_kept_as_before(self, answers):
        fault = {"faultcode": "Server.Session.refresh_access_token.refresh_failed", "faultstring": "Unknown"}
        answers["response"] = httpx.Response(500, json=fault)

        with pytest.raises(WebTritErrorException) as raised:
            await Connector()._send_rest_request("POST", "/rest/Session/refresh_access_token")

        assert raised.value.bss_response_trace["response_content"] == fault
        assert raised.value.bss_response_trace["status_code"] == 500
