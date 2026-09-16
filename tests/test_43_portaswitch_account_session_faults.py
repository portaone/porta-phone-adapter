"""WT-1814 - a session token the answering PortaSwitch site rejects must surface
as 401 access_token_expired, not as a generic 500.

PortaSwitch sessions are site-local, so after a DR failover every token the app
holds was minted by the *other* site. The admin realm logs in again; the account
realm cannot, so AccountAPI.__send_request maps the rejection to a clean 401.
"""

import os
import sys

import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

# PortaSwitchSettings is instantiated at import time and its URL/credential
# fields are mandatory; supply throwaway values before importing the adapter.
os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

from bss.adapters.portaswitch.api.account import AccountAPI  # noqa: E402
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
from bss.adapters.portaswitch.failover import READ_ONLY_FAULTS, SESSION_AUTH_FAULTS  # noqa: E402
from report_error import WebTritErrorException  # noqa: E402

AUTH_FAILED = "Server.Session.check_auth.auth_failed"
BAD_TOKEN = "Client.Session.check_auth.failed_to_process_access_token"
STANDBY_URL = "https://standby.example.com"


def make_api():
    settings = PortaSwitchSettings(
        ADMIN_API_URL="https://main.example.com",
        ACCOUNT_API_URL="https://main.example.com",
        ACCOUNT_API_URL_STANDBY=STANDBY_URL,
        SIP_SERVER_HOST="1.2.3.4",
        ADMIN_API_LOGIN="admin",
        ADMIN_API_TOKEN="token",
    )
    return AccountAPI(settings)


def fault_error(fault_code: str, path: str = "/rest/Account/get_account_info") -> WebTritErrorException:
    """The exception AsyncHTTPAPIConnector.send_rest_request raises for a
    PortaBilling fault: HTTP 500 carrying the faultcode in the response trace."""
    return WebTritErrorException(
        500,
        error_message="Request execution error on the BSS/VoIP system side",
        bss_request_trace={"method": "POST", "url": f"{STANDBY_URL}{path}"},
        bss_response_trace={
            "status_code": 500,
            "text": "Server error '500 Internal Server Error'",
            "response_content": {"faultcode": fault_code, "faultstring": "Authentification failed"},
        },
    )


def raising(api, error):
    """Stub send_rest_request so every call fails with the given exception."""

    async def _send(**kwargs):
        raise error

    api.send_rest_request = _send
    return api


def send(api, **kwargs):
    """Call the name-mangled private __send_request."""
    return api._AccountAPI__send_request(**kwargs)


class TestSessionAuthFaults:
    """A Bearer-authenticated call whose token the site refuses -> 401."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fault_code", [AUTH_FAILED, BAD_TOKEN])
    async def test_bearer_call_reports_expired_token(self, fault_code):
        api = raising(make_api(), fault_error(fault_code))

        with pytest.raises(WebTritErrorException) as raised:
            await send(api, module="Account", method="get_account_info", params={}, access_token="stale")

        assert raised.value.status_code == 401
        assert raised.value.code == "access_token_expired"

    @pytest.mark.asyncio
    async def test_streamed_bearer_call_reports_expired_token(self):
        """Attachments/recordings go through the same path (stream=True)."""
        api = raising(make_api(), fault_error(AUTH_FAILED, "/rest/CDR/get_call_recording"))

        with pytest.raises(WebTritErrorException) as raised:
            await send(
                api, module="CDR", method="get_call_recording", params={"i_xdr": 1}, stream=True, access_token="stale"
            )

        assert raised.value.status_code == 401
        assert raised.value.code == "access_token_expired"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fault_code", [AUTH_FAILED, BAD_TOKEN])
    async def test_session_methods_keep_their_own_errors(self, fault_code):
        """Session/login, /refresh_access_token, /logout and /ping pass the token
        as a parameter, not a header. They must reach the adapter untouched so its
        more specific mappings (refresh_token_invalid, session_close, ...) still
        see the fault code."""
        original = fault_error(fault_code, "/rest/Session/refresh_access_token")
        api = raising(make_api(), original)

        with pytest.raises(WebTritErrorException) as raised:
            await send(api, module="Session", method="refresh_access_token", params={"refresh_token": "rt"})

        assert raised.value is original
        assert raised.value.status_code == 500

    @pytest.mark.asyncio
    async def test_unrelated_fault_passes_through_with_its_trace(self):
        """Any other fault keeps its response trace, so the per-method handlers in
        adapter.py can still extract the fault code and map it."""
        original = fault_error("Server.Account.unified_messaging_disabled")
        api = raising(make_api(), original)

        with pytest.raises(WebTritErrorException) as raised:
            await send(api, module="Account", method="get_mailbox_message_list", params={}, access_token="good")

        assert raised.value is original
        assert (
            raised.value.bss_response_trace["response_content"]["faultcode"]
            == "Server.Account.unified_messaging_disabled"
        )

    @pytest.mark.asyncio
    async def test_read_only_fault_still_maps_to_503(self):
        """The DR read-only degradation added in WT-1654 is unchanged. The two
        fault sets are disjoint (see TestSharedFaultSet), so this pins the
        mapping, not the order the two branches are checked in."""
        api = raising(make_api(), fault_error("standalone_mode"))

        with pytest.raises(WebTritErrorException) as raised:
            await send(api, module="Account", method="set_mailbox_messages_flag", params={}, access_token="good")

        assert raised.value.status_code == 503
        assert raised.value.code == "service_read_only"

    @pytest.mark.asyncio
    async def test_error_without_a_fault_code_is_untouched(self):
        """An exception carrying no PortaBilling fault - a timeout, say - never
        reaches either branch: extract_fault_code re-raises it as it is."""
        original = WebTritErrorException(
            500,
            error_message="Request execution error on the other side",
            bss_request_trace={"method": "POST", "url": f"{STANDBY_URL}/rest/Account/get_account_info"},
            bss_response_trace={"status_code": 408, "text": "Timed out", "response_content": {}},
        )
        api = raising(make_api(), original)

        with pytest.raises(WebTritErrorException) as raised:
            await send(api, module="Account", method="get_account_info", params={}, access_token="stale")

        assert raised.value is original
        assert raised.value.status_code == 500

    @pytest.mark.asyncio
    async def test_no_traces_leak_into_the_401(self):
        """The 401 carries no bss_*_trace, which keeps the subscriber's Bearer
        token out of the response body - and lets extract_fault_code re-raise it
        unchanged through the handlers in adapter.py."""
        api = raising(make_api(), fault_error(AUTH_FAILED))

        with pytest.raises(WebTritErrorException) as raised:
            await send(api, module="Account", method="get_alias_list", params={}, access_token="stale")

        assert raised.value.bss_request_trace is None
        assert raised.value.bss_response_trace is None


class TestSharedFaultSet:
    def test_both_realms_agree_on_the_fault_codes(self):
        """admin.py re-logs in on exactly these codes; account.py reports 401 on
        them. Keep the two realms reading the same set."""
        assert SESSION_AUTH_FAULTS == frozenset({AUTH_FAILED, BAD_TOKEN})

    def test_session_faults_are_not_read_only_faults(self):
        """A rejected token must not be mistaken for a read-only site (503)."""
        assert not (SESSION_AUTH_FAULTS & READ_ONLY_FAULTS)
