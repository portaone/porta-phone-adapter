"""WT-1963 - GET /user/recordings/{recording_id}/transcription.

PortaBilling keys the audio by i_xdr and the transcript by call_recording_id (the
xDR's h323_conf_id), and offers nothing that maps one to the other, so the
recording_id WebTrit hands out carries both. These tests pin what the adapter sends
to CDR/get_transcription and what it answers for an id that cannot carry a transcript.
"""

import base64
import logging
import os
import sys

import httpx
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

from app_config import AppConfig  # noqa: E402
from bss.adapters import BSSAdapter  # noqa: E402
from bss.adapters.portaswitch.adapter import PortaSwitchAdapter  # noqa: E402
from bss.adapters.portaswitch.api.account import AccountAPI  # noqa: E402
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
from bss.adapters.portaswitch.serializer import Serializer  # noqa: E402
from bss.models import CallRecordingId  # noqa: E402
from bss.sessions import SessionInfo  # noqa: E402
from bss.types import Capabilities  # noqa: E402
from report_error import WebTritErrorException  # noqa: E402

CONF_ID = "062BDBD0 7366C3AD 6ED2EA7A 66D2F473"
I_XDR = "129580143"
RECORDING_ID = base64.urlsafe_b64encode(f"{I_XDR}|{CONF_ID}".encode()).decode().rstrip("=")

TRANSCRIPTION = {"call_recording_id": f"{CONF_ID}_0", "segments": [{"text": "hello", "start": 0.0}]}


class FakeAccountAPI:
    """Records the parameters get_call_transcription was called with."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def get_call_transcription(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def _settings() -> PortaSwitchSettings:
    return PortaSwitchSettings(
        ADMIN_API_URL="https://main.example.com",
        ACCOUNT_API_URL="https://main.example.com",
        SIP_SERVER_HOST="1.2.3.4",
        ADMIN_API_LOGIN="admin",
        ADMIN_API_TOKEN="token",
    )


def make_adapter(account_api) -> PortaSwitchAdapter:
    """A PortaSwitchAdapter with its account realm replaced - __init__ builds real
    API connectors and reads the config, so bypass it."""
    adapter = PortaSwitchAdapter.__new__(PortaSwitchAdapter)
    adapter._account_api = account_api
    return adapter


def session() -> SessionInfo:
    return SessionInfo(user_id="john", access_token="valid-token", refresh_token="refresh")


def fault_error(fault_code: str) -> WebTritErrorException:
    """The exception AsyncHTTPAPIConnector.send_rest_request raises for a fault."""
    return WebTritErrorException(
        500,
        error_message="Request execution error on the BSS/VoIP system side",
        bss_request_trace={"method": "POST", "url": "https://pbx.example.com/rest/CDR/get_transcription"},
        bss_response_trace={
            "status_code": 500,
            "text": "Server error '500 Internal Server Error'",
            "response_content": {"faultcode": fault_code, "faultstring": "Not found"},
        },
    )


class TestRequestParameters:
    @pytest.mark.asyncio
    async def test_the_conf_id_half_of_the_recording_id_is_sent(self):
        api = FakeAccountAPI(result=TRANSCRIPTION)
        adapter = make_adapter(api)

        result = await adapter.retrieve_call_transcription(session(), CallRecordingId(RECORDING_ID))

        assert result == TRANSCRIPTION
        assert api.calls[0]["call_recording_id"] == CONF_ID
        assert api.calls[0]["access_token"] == "valid-token"

    @pytest.mark.asyncio
    async def test_format_and_check_only_are_passed_through(self):
        api = FakeAccountAPI(result={})
        adapter = make_adapter(api)

        await adapter.retrieve_call_transcription(
            session(), CallRecordingId(RECORDING_ID), format="text", check_only=True
        )

        assert api.calls[0]["format"] == "text"
        assert api.calls[0]["check_only"] is True

    @pytest.mark.asyncio
    async def test_a_streamed_answer_is_handed_over_untouched(self):
        """format=text (or a multi-file call) comes back as (content_type, iterator)."""
        streamed = ("text/plain", iter([b"hello"]))
        adapter = make_adapter(FakeAccountAPI(result=streamed))

        assert await adapter.retrieve_call_transcription(session(), CallRecordingId(RECORDING_ID)) is streamed


class TestIdsWithoutATranscriptionKey:
    @pytest.mark.asyncio
    async def test_a_pre_wt_1963_id_answers_404(self):
        """A bare i_xdr still downloads the audio, but no PortaBilling method turns
        it into a call_recording_id - the client has to re-read the call history."""
        api = FakeAccountAPI(result=TRANSCRIPTION)
        adapter = make_adapter(api)

        with pytest.raises(WebTritErrorException) as raised:
            await adapter.retrieve_call_transcription(session(), CallRecordingId(I_XDR))

        assert raised.value.status_code == 404
        assert api.calls == []

    @pytest.mark.asyncio
    async def test_a_malformed_id_answers_422(self):
        api = FakeAccountAPI(result=TRANSCRIPTION)
        adapter = make_adapter(api)

        with pytest.raises(WebTritErrorException) as raised:
            await adapter.retrieve_call_transcription(session(), CallRecordingId("not an id at all!"))

        assert raised.value.status_code == 422
        assert api.calls == []


class TestFaultMapping:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fault_code",
        [
            "Server.CDR.xdr_not_found",
            "Server.CDR.invalid_call_recording_id",
            # A recorded call that was never transcribed - the service was off when it
            # happened - or whose transcript is still being produced.
            "Server.CDR.transcription_not_found",
        ],
    )
    async def test_unknown_recording_answers_404(self, fault_code):
        adapter = make_adapter(FakeAccountAPI(error=fault_error(fault_code)))

        with pytest.raises(WebTritErrorException) as raised:
            await adapter.retrieve_call_transcription(session(), CallRecordingId(RECORDING_ID))

        assert raised.value.status_code == 404

    @pytest.mark.asyncio
    async def test_a_stale_session_token_answers_401(self):
        adapter = make_adapter(
            FakeAccountAPI(error=fault_error("Client.Session.check_auth.failed_to_process_access_token"))
        )

        with pytest.raises(WebTritErrorException) as raised:
            await adapter.retrieve_call_transcription(session(), CallRecordingId(RECORDING_ID))

        assert raised.value.status_code == 401
        assert raised.value.code == "access_token_expired"

    @pytest.mark.asyncio
    async def test_any_other_fault_passes_through(self):
        original = fault_error("Server.CDR.something_else")
        adapter = make_adapter(FakeAccountAPI(error=original))

        with pytest.raises(WebTritErrorException) as raised:
            await adapter.retrieve_call_transcription(session(), CallRecordingId(RECORDING_ID))

        assert raised.value is original


def capabilities_of(**config):
    """Capabilities the real PortaSwitch adapter reports under this config."""
    subject = object.__new__(PortaSwitchAdapter)
    subject.config = AppConfig({"Capabilities": config})
    return set(subject.calculate_capabilities())


class TestCapability:
    def test_the_adapter_codes_for_it(self):
        assert Capabilities.transcription in PortaSwitchAdapter.CAPABILITIES

    def test_off_when_the_deployment_says_nothing(self):
        # Transcription is a PortaSwitch service feature the deployment subscribes to
        # and is charged for; advertising it where it is not configured makes clients
        # offer a control that answers 404.
        assert Capabilities.transcription not in capabilities_of(RECORDINGS="1")

    def test_the_switch_turns_it_on(self):
        assert Capabilities.transcription in capabilities_of(RECORDINGS="1", TRANSCRIPTION="1")

    def test_it_follows_recordings_off(self):
        # The id a transcript is asked for comes from a call history row that has a
        # recording; with recordings off there is nothing to ask about.
        assert Capabilities.transcription not in capabilities_of(RECORDINGS="0", TRANSCRIPTION="1")

    def test_the_option_name_matches_the_env_var(self):
        option = BSSAdapter.CONFIG_CAPABILITIES_OPTIONS["TRANSCRIPTION"]

        assert option["option"] == Capabilities.transcription
        assert option["default"] is False

    def test_the_wire_value(self):
        assert Capabilities.transcription.value == "transcription"


class TestParamsSentToPortaBilling:
    @pytest.mark.asyncio
    async def test_check_only_is_omitted_unless_asked_for(self):
        """PortaBilling defaults check_only to 0 and format to json; send neither
        unless the caller asked, so the billing keeps deciding the default."""
        api = AccountAPI(_settings())
        sent = {}

        async def _send(**kwargs):
            sent.update(kwargs)
            return TRANSCRIPTION

        api.send_rest_request = _send

        await api.get_call_transcription(call_recording_id=CONF_ID, access_token="valid-token")

        assert sent["path"] == "/rest/CDR/get_transcription"
        assert sent["json"] == {"params": {"call_recording_id": CONF_ID}}
        assert sent["headers"] == {"Authorization": "Bearer valid-token"}
        assert sent["stream"] is True

    @pytest.mark.asyncio
    async def test_check_only_is_sent_as_the_integer_portabilling_expects(self):
        api = AccountAPI(_settings())
        sent = {}

        async def _send(**kwargs):
            sent.update(kwargs)
            return {}

        api.send_rest_request = _send

        await api.get_call_transcription(
            call_recording_id=CONF_ID, access_token="valid-token", format="text", check_only=True
        )

        assert sent["json"]["params"] == {"call_recording_id": CONF_ID, "format": "text", "check_only": 1}


class TestTheAudioDownloadStillWorks:
    """The recording_id grew a second key in WT-1963; CDR/get_call_recording is still
    keyed by i_xdr alone and must be handed that half, not the whole token."""

    @pytest.mark.asyncio
    async def test_the_i_xdr_half_is_what_reaches_the_billing(self):
        calls = []

        class FakeRecordingAPI:
            async def get_call_recording(self, **kwargs):
                calls.append(kwargs)
                return ("audio/mpeg", iter([b""]))

        adapter = make_adapter(FakeRecordingAPI())

        await adapter.retrieve_call_recording(session(), CallRecordingId(RECORDING_ID))

        assert calls[0]["recording_id"] == I_XDR

    @pytest.mark.asyncio
    async def test_a_pre_wt_1963_id_still_downloads(self):
        calls = []

        class FakeRecordingAPI:
            async def get_call_recording(self, **kwargs):
                calls.append(kwargs)
                return ("audio/mpeg", iter([b""]))

        adapter = make_adapter(FakeRecordingAPI())

        await adapter.retrieve_call_recording(session(), CallRecordingId(I_XDR))

        assert calls[0]["recording_id"] == I_XDR

    @pytest.mark.asyncio
    async def test_a_malformed_id_answers_422(self):
        adapter = make_adapter(FakeAccountAPI())

        with pytest.raises(WebTritErrorException) as raised:
            await adapter.retrieve_call_recording(session(), CallRecordingId("not an id at all!"))

        assert raised.value.status_code == 422


class TestSerializerHandsOutAUsableId:
    def test_the_history_id_round_trips_into_both_keys(self):
        assert Serializer.parse_recording_id(RECORDING_ID) == (I_XDR, CONF_ID)

    def test_a_recorded_call_without_a_conf_id_is_logged(self, caplog):
        """The fallback to a bare i_xdr makes every transcription request answer 404.
        If the billing ever stops returning h323_conf_id by default, this is the only
        thing that says why."""
        cdr = {"i_xdr": 129580143, "cr_download_ids": [f"{CONF_ID}_0"]}

        with caplog.at_level(logging.WARNING):
            assert Serializer.compose_recording_id(cdr) == I_XDR

        assert "h323_conf_id" in caplog.text


class TestPlainTextIsOnlyATranscription:
    """decode_response turns a text/* body into a stream - but only for the one method
    that legitimately answers with one."""

    class FakeResponse:
        def __init__(self, path, content_type):
            self.headers = {"Content-Type": content_type}
            self.request = httpx.Request("POST", f"https://pbx.example.com{path}")
            self.closed = False

        async def aclose(self):
            self.closed = True

    @pytest.mark.asyncio
    async def test_a_transcription_streams(self):
        api = AccountAPI(_settings())
        response = self.FakeResponse("/rest/CDR/get_transcription", "text/plain; charset=utf-8")

        content_type, _ = await api.decode_response(response)

        assert content_type.startswith("text/plain")
        assert response.closed is False

    @pytest.mark.asyncio
    async def test_a_text_body_from_any_other_call_is_still_an_error(self):
        # An error page from something in between must not reach the client as a 200
        # carrying that page as the recording.
        api = AccountAPI(_settings())
        response = self.FakeResponse("/rest/CDR/get_call_recording", "text/html")

        with pytest.raises(ValueError):
            await api.decode_response(response)

        assert response.closed is True
