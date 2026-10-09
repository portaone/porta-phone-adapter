"""WT-1993 - public links to a call recording and its transcript.

The link is served with the admin token, so it is the only credential, and it must
only ever be issued for the caller's own call. These tests pin the signing, the
ownership check made with the caller's token, and what the admin-realm fetch answers.
"""

import base64
import os
import sys
import time

import httpx
import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

from bss.adapters.portaswitch.adapter import PortaSwitchAdapter  # noqa: E402
from bss.adapters.portaswitch.api.admin import AdminAPI  # noqa: E402
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
from bss.adapters.portaswitch.recording_link import (  # noqa: E402
    RecordingLinkExpired,
    RecordingLinkInvalid,
    RecordingLinkSigner,
)
from bss.models import CallRecordingId  # noqa: E402
from bss.sessions import SessionInfo  # noqa: E402
from bss.types import Capabilities  # noqa: E402
from report_error import WebTritErrorException  # noqa: E402

CONF_ID = "062BDBD0 7366C3AD 6ED2EA7A 66D2F473"
I_XDR = "129580143"
RECORDING_ID = base64.urlsafe_b64encode(f"{I_XDR}|{CONF_ID}".encode()).decode().rstrip("=")
SECRET = "s" * 64
BILLING = "https://pbx.example.com"


def fault_error(fault_code: str) -> WebTritErrorException:
    return WebTritErrorException(
        500,
        error_message="Request execution error on the BSS/VoIP system side",
        bss_request_trace={"method": "POST", "url": "https://pbx.example.com/rest/CDR/get_call_recording"},
        bss_response_trace={
            "status_code": 500,
            "text": "Server error '500 Internal Server Error'",
            "response_content": {"faultcode": fault_code, "faultstring": "Fault"},
        },
    )


class Stream:
    """A streamed answer that records whether its connection was released."""

    def __init__(self):
        self.closed = False

    def result(self):
        async def chunks():
            try:
                yield b"RIFF"
                yield b"more"
            finally:
                self.closed = True

        return "audio/wav", chunks()


class FakeAccountAPI:
    def __init__(self, recording_error=None, transcription_error=None):
        self.recording_error = recording_error
        self.transcription_error = transcription_error
        self.recording = Stream()
        self.calls = []

    async def get_call_recording(self, **kwargs):
        self.calls.append(("recording", kwargs))
        if self.recording_error:
            raise self.recording_error
        return self.recording.result()

    async def get_call_transcription(self, **kwargs):
        self.calls.append(("transcription", kwargs))
        if self.transcription_error:
            raise self.transcription_error
        return {"call_recording_id": CONF_ID}


class FakeAdminAPI:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def get_call_recording(self, i_xdr):
        self.calls.append(("recording", i_xdr))
        if self.error:
            raise self.error
        return Stream().result()

    async def get_call_transcription(self, call_recording_id, format=None, check_only=False):
        self.calls.append(("transcription", call_recording_id, format, check_only))
        if self.error:
            raise self.error
        return {"call_recording_id": call_recording_id}


def make_adapter(account_api=None, admin_api=None, transcription=True, secret=SECRET) -> PortaSwitchAdapter:
    adapter = PortaSwitchAdapter.__new__(PortaSwitchAdapter)
    adapter._account_api = account_api or FakeAccountAPI()
    adapter._admin_api = admin_api or FakeAdminAPI()
    adapter._recording_link_signer = RecordingLinkSigner(secret, BILLING) if secret else None
    adapter._cached_capabilities = [Capabilities.recordings] + ([Capabilities.transcription] if transcription else [])
    return adapter


def session() -> SessionInfo:
    return SessionInfo(user_id="john", access_token="valid-token", refresh_token="refresh")


class TestSigner:
    def test_round_trip(self):
        signer = RecordingLinkSigner(SECRET, BILLING)

        link = signer.verify(signer.sign(129580143, CONF_ID, ttl=60))

        assert link.i_xdr == 129580143
        assert link.call_recording_id == CONF_ID
        assert link.expires_at > time.time()

    def test_no_transcript_key_and_no_expiry(self):
        signer = RecordingLinkSigner(SECRET, BILLING)

        link = signer.verify(signer.sign(1, None))

        assert link.call_recording_id is None
        assert link.expires_at == 0

    def test_fits_a_crm_field(self):
        # Freshdesk and Zoho cap a text field at 255 characters, and the host, path
        # and query string have to fit beside the id.
        assert len(RecordingLinkSigner(SECRET, BILLING).sign(2**63 - 1, CONF_ID, ttl=10 * 365 * 24 * 3600)) <= 120

    def test_another_secret_does_not_verify_it(self):
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(1, CONF_ID)

        with pytest.raises(RecordingLinkInvalid):
            RecordingLinkSigner("other", BILLING).verify(link_id)

    def test_another_billing_does_not_verify_it(self):
        # The same i_xdr is another call on another PortaBilling.
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(1, CONF_ID)

        with pytest.raises(RecordingLinkInvalid):
            RecordingLinkSigner(SECRET, "https://other.example.com").verify(link_id)

    @pytest.mark.parametrize("ttl", [-1, 10 * 365 * 24 * 3600 + 1])
    def test_a_ttl_out_of_range_is_refused(self, ttl):
        with pytest.raises(ValueError):
            RecordingLinkSigner(SECRET, BILLING).sign(1, CONF_ID, ttl=ttl)

    def test_an_altered_id_does_not_verify(self):
        signer = RecordingLinkSigner(SECRET, BILLING)
        raw = bytearray(base64.urlsafe_b64decode(signer.sign(1, CONF_ID) + "=="))
        raw[5] ^= 1  # another i_xdr
        tampered = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")

        with pytest.raises(RecordingLinkInvalid):
            signer.verify(tampered)

    @pytest.mark.parametrize("link_id", ["", "x", "!!!", "AAAA", RECORDING_ID])
    def test_garbage_does_not_verify(self, link_id):
        with pytest.raises(RecordingLinkInvalid):
            RecordingLinkSigner(SECRET, BILLING).verify(link_id)

    def test_an_expired_link(self, monkeypatch):
        signer = RecordingLinkSigner(SECRET, BILLING)
        link_id = signer.sign(1, CONF_ID, ttl=60)
        monkeypatch.setattr(time, "time", lambda: 2**31)

        with pytest.raises(RecordingLinkExpired):
            signer.verify(link_id)


class TestIssue:
    @pytest.mark.asyncio
    async def test_both_halves_are_checked_with_the_callers_token(self):
        account = FakeAccountAPI()
        adapter = make_adapter(account_api=account)

        link_id, transcription = await adapter.issue_recording_link(session(), CallRecordingId(RECORDING_ID), ttl=60)

        assert [kind for kind, _ in account.calls] == ["recording", "transcription"]
        assert all(kwargs["access_token"] == "valid-token" for _, kwargs in account.calls)
        assert account.calls[1][1]["check_only"] is True
        link = RecordingLinkSigner(SECRET, BILLING).verify(link_id)
        assert (link.i_xdr, link.call_recording_id) == (int(I_XDR), CONF_ID)
        assert transcription is True

    @pytest.mark.asyncio
    async def test_the_recording_is_not_downloaded(self):
        account = FakeAccountAPI()

        await make_adapter(account_api=account).issue_recording_link(session(), CallRecordingId(RECORDING_ID))

        assert account.recording.closed

    @pytest.mark.asyncio
    async def test_another_accounts_recording_answers_403(self):
        account = FakeAccountAPI(recording_error=fault_error("Server.CDR.forbidden_account_access"))

        with pytest.raises(WebTritErrorException) as exc:
            await make_adapter(account_api=account).issue_recording_link(session(), CallRecordingId(RECORDING_ID))

        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_another_accounts_transcript_answers_403(self):
        # Own i_xdr glued to another call's h323_conf_id.
        account = FakeAccountAPI(transcription_error=fault_error("Server.CDR.forbidden_account_access"))

        with pytest.raises(WebTritErrorException) as exc:
            await make_adapter(account_api=account).issue_recording_link(session(), CallRecordingId(RECORDING_ID))

        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_transcript_still_on_its_way_keeps_its_key(self):
        account = FakeAccountAPI(transcription_error=fault_error("Server.CDR.transcription_not_found"))

        link_id, transcription = await make_adapter(account_api=account).issue_recording_link(
            session(), CallRecordingId(RECORDING_ID)
        )

        assert RecordingLinkSigner(SECRET, BILLING).verify(link_id).call_recording_id == CONF_ID
        assert transcription is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fault_code", ["Server.CDR.xdr_not_found", "Server.CDR.invalid_call_recording_id"])
    async def test_a_transcript_key_the_billing_does_not_know_is_dropped(self, fault_code):
        # It cannot be shown to be the caller's, so it does not go into the link.
        account = FakeAccountAPI(transcription_error=fault_error(fault_code))

        link_id, transcription = await make_adapter(account_api=account).issue_recording_link(
            session(), CallRecordingId(RECORDING_ID)
        )

        assert RecordingLinkSigner(SECRET, BILLING).verify(link_id).call_recording_id is None
        assert transcription is False

    @pytest.mark.asyncio
    async def test_without_the_transcription_capability_the_link_carries_no_transcript(self):
        account = FakeAccountAPI()

        link_id, transcription = await make_adapter(account_api=account, transcription=False).issue_recording_link(
            session(), CallRecordingId(RECORDING_ID)
        )
        assert transcription is False

        assert [kind for kind, _ in account.calls] == ["recording"]
        assert RecordingLinkSigner(SECRET, BILLING).verify(link_id).call_recording_id is None

    @pytest.mark.asyncio
    async def test_a_pre_wt_1963_id_links_the_audio_only(self):
        link_id, _ = await make_adapter().issue_recording_link(session(), CallRecordingId(I_XDR))

        assert RecordingLinkSigner(SECRET, BILLING).verify(link_id).call_recording_id is None

    @pytest.mark.asyncio
    async def test_a_malformed_id_answers_422(self):
        with pytest.raises(WebTritErrorException) as exc:
            await make_adapter().issue_recording_link(session(), CallRecordingId("not!an!id"))

        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_without_secret_key_base_answers_501(self):
        with pytest.raises(WebTritErrorException) as exc:
            await make_adapter(secret=None).issue_recording_link(session(), CallRecordingId(RECORDING_ID))

        assert exc.value.status_code == 501


class TestPublicFetch:
    @pytest.mark.asyncio
    async def test_the_recording_comes_from_the_admin_realm(self):
        admin = FakeAdminAPI()
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(int(I_XDR), CONF_ID)

        content_type, _ = await make_adapter(admin_api=admin).retrieve_linked_call_recording(link_id)

        assert content_type == "audio/wav"
        assert admin.calls == [("recording", int(I_XDR))]

    @pytest.mark.asyncio
    async def test_the_transcript_comes_from_the_admin_realm(self):
        admin = FakeAdminAPI()
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(int(I_XDR), CONF_ID)

        await make_adapter(admin_api=admin).retrieve_linked_call_transcription(link_id, "text", True)

        assert admin.calls == [("transcription", CONF_ID, "text", True)]

    @pytest.mark.asyncio
    async def test_a_link_without_a_transcript_answers_404_for_it(self):
        admin = FakeAdminAPI()
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(int(I_XDR), None)

        with pytest.raises(WebTritErrorException) as exc:
            await make_adapter(admin_api=admin).retrieve_linked_call_transcription(link_id)

        assert exc.value.status_code == 404
        assert exc.value.code == "transcription_not_found"
        assert admin.calls == []

    @pytest.mark.asyncio
    async def test_a_forged_link_answers_404_without_reaching_the_billing(self):
        admin = FakeAdminAPI()
        link_id = RecordingLinkSigner("someone else", BILLING).sign(int(I_XDR), CONF_ID)

        with pytest.raises(WebTritErrorException) as exc:
            await make_adapter(admin_api=admin).retrieve_linked_call_recording(link_id)

        assert exc.value.status_code == 404
        assert admin.calls == []

    @pytest.mark.asyncio
    async def test_an_expired_link_answers_410(self, monkeypatch):
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(int(I_XDR), CONF_ID, ttl=60)
        monkeypatch.setattr(time, "time", lambda: 2**31)

        with pytest.raises(WebTritErrorException) as exc:
            await make_adapter().retrieve_linked_call_recording(link_id)

        assert exc.value.status_code == 410

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fault_code",
        ["Server.CDR.xdr_not_found", "Server.CDR.invalid_call_recording_id"],
    )
    async def test_a_deleted_recording_answers_404(self, fault_code):
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(int(I_XDR), CONF_ID)
        adapter = make_adapter(admin_api=FakeAdminAPI(error=fault_error(fault_code)))

        with pytest.raises(WebTritErrorException) as exc:
            await adapter.retrieve_linked_call_recording(link_id)

        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fault_code", ["Server.CDR.transcription_not_found", "Server.CDR.invalid_call_recording_id"]
    )
    async def test_a_missing_transcript_is_told_apart_from_a_dead_link(self, fault_code):
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(int(I_XDR), CONF_ID)
        adapter = make_adapter(admin_api=FakeAdminAPI(error=fault_error(fault_code)))

        with pytest.raises(WebTritErrorException) as exc:
            await adapter.retrieve_linked_call_transcription(link_id)

        assert exc.value.status_code == 404
        assert exc.value.code == "transcription_not_found"

    @pytest.mark.asyncio
    async def test_a_deleted_call_answers_a_dead_link(self):
        link_id = RecordingLinkSigner(SECRET, BILLING).sign(int(I_XDR), CONF_ID)
        adapter = make_adapter(admin_api=FakeAdminAPI(error=fault_error("Server.CDR.xdr_not_found")))

        with pytest.raises(WebTritErrorException) as exc:
            await adapter.retrieve_linked_call_transcription(link_id)

        assert exc.value.code == "recording_link_not_found"


def _admin_api() -> AdminAPI:
    return AdminAPI(
        PortaSwitchSettings(
            ADMIN_API_URL="https://pbx.example.com",
            ACCOUNT_API_URL="https://pbx.example.com",
            SIP_SERVER_HOST="1.2.3.4",
            ADMIN_API_LOGIN="admin",
            ADMIN_API_TOKEN="token",
        )
    )


class TestAdminRealmDecoding:
    @pytest.mark.asyncio
    async def test_a_recording_is_streamed(self):
        request = httpx.Request("POST", "https://pbx.example.com/rest/CDR/get_call_recording")
        response = httpx.Response(
            200,
            headers={"Content-Type": "audio/mpeg", "Content-Disposition": "attachment; filename=a.mp3"},
            content=b"ID3",
            request=request,
        )

        content_type, iterator = await _admin_api().decode_response(response)

        assert content_type == "audio/mpeg"
        assert b"".join([chunk async for chunk in iterator]) == b"ID3"

    @pytest.mark.asyncio
    async def test_every_other_method_is_json_as_before(self):
        request = httpx.Request("POST", "https://pbx.example.com/rest/Account/get_account_info")
        response = httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b'{"ok": 1}', request=request)

        assert await _admin_api().decode_response(response) == {"ok": 1}
