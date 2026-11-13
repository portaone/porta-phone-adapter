"""The NetSapiens adapter against a stubbed NetSapiens API (WT-2299).

The answers below are shaped like the ones the NetSapiens developer sandbox (v45) gave:
devices carry the SIP password, contacts carry no registration state, DIDs live only in
/phonenumbers, CDR user fields are numbers, voicemail sits in the "new" / "save" folders.
"""
import json
import os
import sys
import time

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

import pytest
from jose import jwt as jose_jwt

import bss.adapters.netsapiens as netsapiens
from app_config import AppConfig
from bss.dbs import TiedKeyValue
from bss.sessions import SessionStorage
from bss.types import (Capabilities, ConnectStatus, Direction, SIPRegistrationStatus, UserInfo,
                       ExtendedUserInfo, UserVoicemailMessagePatch)
from report_error import WebTritErrorException

DOMAIN = "northwind.example"
SSO_SECRET = "sso-shared-secret"

DEVICES = {
    "1001": [{"device": "1001test", "device-sip-registration-password": "other"},
             {"device": "1001a", "user": "1001", "domain": DOMAIN,
              "device-sip-registration-uri": f"sip:1001a@{DOMAIN}",
              "device-sip-registration-password": "sip-secret", "core-server": ""}],
}
CONTACTS = [
    {"uid": f"1000@{DOMAIN}", "user": "1000", "domain": DOMAIN, "name-first-name": "Volodymyr", "email": ""},
    {"uid": f"1001@{DOMAIN}", "user": "1001", "domain": DOMAIN, "name-first-name": "Jane", "name-last-name": "Cooper"},
    {"uid": f"1002@{DOMAIN}", "user": "1002", "domain": DOMAIN, "name-first-name": "Mark", "name-last-name": "Lee"},
]
DOMAIN_DEVICES = [{"user": "1002", "device": "1002a", "device-sip-registration-state": "registered"},
                  {"user": "1000", "device": "1000", "device-sip-registration-state": "unregistered"}]
PHONENUMBERS = [{"phonenumber": "14155550101", "dial-rule-translation-destination-user": "1001"},
                {"phonenumber": "14155550102", "dial-rule-translation-destination-user": "1002"}]
CDRS = [
    # Jane called Mark, answered
    {"call-orig-call-id": "c1", "call-orig-user": 1001, "call-orig-to-user": "1002", "call-orig-request-user": 1002,
     "call-orig-caller-id": "1001a", "call-direction": 1, "call-start-datetime": "2026-11-13T11:31:30+00:00",
     "call-answer-datetime": "2026-11-13T11:31:46+00:00", "call-talking-duration-seconds": 16,
     "call-total-duration-seconds": 32, "call-disconnect-reason-text": "Term: Bye"},
    # Mark called Jane, she did not answer
    {"call-orig-call-id": "c2", "call-orig-user": 1002, "call-orig-to-user": "1001", "call-orig-request-user": 1001,
     "call-orig-caller-id": "1002a", "call-direction": 2, "call-start-datetime": "2026-11-13T11:37:36+00:00",
     "call-talking-duration-seconds": 0, "call-total-duration-seconds": 9,
     "call-disconnect-reason-text": "Orig: Cancel"},
    # Jane called Mark, she hung up while it rang
    {"call-orig-call-id": "c3", "call-orig-user": 1001, "call-orig-to-user": "1002", "call-orig-request-user": 1002,
     "call-direction": 2, "call-start-datetime": "2026-11-13T11:21:39+00:00", "call-talking-duration-seconds": 0,
     "call-total-duration-seconds": 6, "call-disconnect-reason-text": "Orig: Cancel"},
]
VOICEMAIL = {"new": [{"filename": "vm-1.wav", "file-duration-seconds": 11, "file-size-kilobytes": "57.86 KB",
                      "created-datetime": "2026-11-13T11:51:07+00:00", "voicemail-from-user": "1002",
                      "file-access-url": "https://ns.example/audio/vm-1.wav"}],
             "save": []}


class FakeResponse:
    def __init__(self, body, status_code=200, content=b""):
        self.body = body
        self.status_code = status_code
        self.text = json.dumps(body)
        self.content = content
        self.headers = {"Content-Type": "audio/wav"}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self.body

    def iter_content(self, size):
        yield self.content


class FakeNetSapiens:
    def __init__(self):
        self.jwt_requests = []
        self.auth = []
        self.moved = []

    def __call__(self, method, url, timeout=None, headers=None, json=None, **kwargs):
        self.auth.append((url, (headers or {}).get("Authorization")))
        path = url.split("/ns-api/v2", 1)[-1].split("?")[0]
        if path == "/jwt":
            self.jwt_requests.append(json)
            return FakeResponse({"token": f"UT-{json['user']}", "refresh": "R"})
        if path.endswith("/devices") and "/users/" in path:
            return FakeResponse(DEVICES.get(path.split("/users/")[1].split("/")[0], []))
        if path.endswith("/contacts"):
            return FakeResponse(CONTACTS)
        if path == f"/domains/{DOMAIN}/devices":
            return FakeResponse(DOMAIN_DEVICES)
        if path == f"/domains/{DOMAIN}/phonenumbers":
            return FakeResponse(PHONENUMBERS)
        if path.endswith("/cdrs"):
            return FakeResponse(CDRS)
        if "/recordings/" in path:
            if path.endswith("/c1"):
                return FakeResponse({"call-id": "c1", "call-recording-status": "converted",
                                     "file-access-url": "https://ns.example/rec/c1"})
            return FakeResponse({"code": 404}, status_code=404)
        if "/voicemails/" in path:
            if method == "PATCH" and path.endswith("/save"):
                self.moved.append(path)
                return FakeResponse({}, status_code=202)
            folder = path.rsplit("/", 1)[-1]
            return FakeResponse(VOICEMAIL.get(folder, []))
        if url.startswith("https://ns.example/"):
            return FakeResponse(None, content=b"RIFF....WAVE")
        return FakeResponse({"code": 404}, status_code=404)


@pytest.fixture
def ns(monkeypatch):
    fake = FakeNetSapiens()
    monkeypatch.setattr(netsapiens.requests, "request", fake)
    monkeypatch.setattr(netsapiens.requests, "post", lambda url, **kw: fake("POST", url, **kw))
    monkeypatch.setattr(netsapiens.requests, "get", lambda url, **kw: fake("GET", url, **kw))
    monkeypatch.setattr(netsapiens.shutil, "which", lambda name: None)
    return fake


def make_adapter(monkeypatch, oauth=False, sso=True):
    client = {"domain": DOMAIN, "api_server": "https://ns.example", "api_key": "domain-key",
              "device_filter": "a", "default_sip_outbound_proxy": "core.ns.example"}
    if oauth:
        client.update(client_id="cid", client_secret="csecret")
    monkeypatch.setenv("NETSAPIENS_CLIENTS", json.dumps([client]))
    monkeypatch.setenv("NETSAPIENS_TOKEN_STORAGE", "memory")
    if sso:
        monkeypatch.setenv("NETSAPIENS_SSO_SECRET", SSO_SECRET)
        monkeypatch.setenv("NETSAPIENS_SSO_ISSUER", "your-saas")
    else:
        monkeypatch.delenv("NETSAPIENS_SSO_SECRET", raising=False)
    monkeypatch.setattr(netsapiens, "configure_session_storage",
                        lambda config: SessionStorage(session_db=TiedKeyValue()))
    subject = netsapiens.NetsapiensAdapter(AppConfig({}))
    subject.initialize()
    return subject


@pytest.fixture
def adapter(monkeypatch, ns):
    return make_adapter(monkeypatch)


def sso_token(sub=f"1001@{DOMAIN}", **over):
    secret = over.pop("secret", SSO_SECRET)
    claims = {"iss": "your-saas", "aud": "portadialer", "sub": sub, "exp": int(time.time()) + 120,
              "jti": f"j-{time.time_ns()}"}
    claims.update(over)
    return jose_jwt.encode(claims, secret, algorithm="HS256")


class TestCapabilities:
    def test_without_an_oauth_client_there_is_no_password_sign_in(self, adapter):
        caps = set(adapter.get_capabilities())
        assert Capabilities.passwordSignin not in caps
        assert {Capabilities.autoProvision, Capabilities.customMethods, Capabilities.voicemail,
                Capabilities.recordings, Capabilities.direct_presence} <= caps
        assert Capabilities.signup not in caps

    def test_with_an_oauth_client_and_no_sso(self, monkeypatch, ns):
        caps = set(make_adapter(monkeypatch, oauth=True, sso=False).get_capabilities())
        assert Capabilities.passwordSignin in caps
        assert Capabilities.autoProvision not in caps
        assert Capabilities.customMethods not in caps


class TestPasswordSignIn:
    def test_refused_without_an_oauth_client(self, adapter):
        # NetSapiens could not check the password, so it must not be accepted
        with pytest.raises(WebTritErrorException) as error:
            adapter.authenticate(ExtendedUserInfo(user_id="N/A", login=f"1001@{DOMAIN}", tenant_id="default"), "x")
        assert error.value.status_code == 401


class TestSingleSignOn:
    def test_a_saas_token_signs_in_without_a_password(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        assert session.user_id.root == f"1001@{DOMAIN}"
        assert ns.jwt_requests == [{"user": "1001", "domain": DOMAIN}]

    def test_a_token_works_once(self, adapter):
        token = sso_token()
        adapter.autoprovision_session(token)
        with pytest.raises(WebTritErrorException) as error:
            adapter.autoprovision_session(token)
        assert error.value.status_code == 401

    @pytest.mark.parametrize("over", [{"secret": "wrong"}, {"exp": 1}, {"aud": "someone-else"},
                                      {"iss": "not-your-saas"}, {"sub": "1001@unknown.example"}])
    def test_a_bad_token_is_refused(self, adapter, over):
        with pytest.raises(WebTritErrorException) as error:
            adapter.autoprovision_session(sso_token(**over))
        assert error.value.status_code == 401

    def test_the_embedded_sign_in_page_goes_through_signup(self, adapter):
        session = adapter.signup({"sso_token": sso_token()})
        assert session.user_id.root == f"1001@{DOMAIN}"


class TestUser:
    def test_sip_account_dids_and_lists(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        user = adapter.retrieve_user(session, UserInfo(user_id=f"1001@{DOMAIN}"))

        assert user.sip.username == "1001a"
        assert user.sip.password == "sip-secret"
        assert user.sip.registrar_server.host == "core.ns.example"
        assert user.numbers.main == "+14155550101"
        assert user.numbers.ext == "1001"
        assert user.numbers.sms == [] and user.numbers.additional == []

    def test_the_token_from_sign_in_is_reused(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"))
        adapter.retrieve_contacts(session, UserInfo(user_id=f"1001@{DOMAIN}"))
        assert len(ns.jwt_requests) == 1


class TestContacts:
    def test_ids_and_registration_state(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        contacts = adapter.retrieve_contacts(session, UserInfo(user_id=f"1001@{DOMAIN}"))

        assert [c.user_id.root for c in contacts] == [f"1000@{DOMAIN}", f"1002@{DOMAIN}"]
        assert {c.numbers.ext: c.sip_status for c in contacts} == {
            "1000": SIPRegistrationStatus.notregistered, "1002": SIPRegistrationStatus.registered}

    @pytest.mark.parametrize("kwargs, expected", [
        ({"phone_numbers": ["1002"]}, ["1002"]),
        ({"search": "mark"}, ["1002"]),
        ({"search": "100"}, ["1000", "1002"]),
        ({"items_per_page": 1, "page": 2}, ["1002"]),
    ])
    def test_v2_filters(self, adapter, kwargs, expected):
        session = adapter.autoprovision_session(sso_token())
        items, total = adapter.retrieve_contacts_v2(session, UserInfo(user_id=f"1001@{DOMAIN}"), **kwargs)
        assert [c.numbers.ext for c in items] == expected


class TestCallHistory:
    def test_direction_status_and_duration_from_janes_side(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        items, total = adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"))

        assert total == 3
        by_id = {i.call_id: i for i in items}
        assert (by_id["c1"].direction, by_id["c1"].status, by_id["c1"].duration) == (
            Direction.outgoing, ConnectStatus.accepted, 16)
        assert (by_id["c1"].caller, by_id["c1"].callee) == ("1001", "1002")
        assert (by_id["c2"].direction, by_id["c2"].status) == (Direction.incoming, ConnectStatus.missed)
        assert (by_id["c3"].direction, by_id["c3"].status) == (Direction.outgoing, ConnectStatus.declined)

    def test_newest_first_with_a_real_total(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        items, total = adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"), page=1, items_per_page=2)
        assert [i.call_id for i in items] == ["c2", "c1"]
        assert total == 3

    def test_recorded_calls_carry_a_recording_id(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        items, _ = adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"))
        assert {i.call_id: i.recording_id for i in items} == {"c1": "c1", "c2": None, "c3": None}

    def test_the_recording_downloads(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        content_type, body = adapter.retrieve_call_recording(session, "c1")
        assert content_type == "audio/wav"
        assert b"".join(body) == b"RIFF....WAVE"


class TestVoicemail:
    def test_list_and_mark_heard(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        box = adapter.retrieve_voicemails(session, UserInfo(user_id=f"1001@{DOMAIN}"))
        assert box.has_new_messages
        msg = box.messages[0]
        assert (msg.id, msg.duration, msg.sender, msg.seen) == ("vm-1.wav", 11.0, "1002", False)

        result = adapter.patch_voicemail_message(session, "vm-1.wav", UserVoicemailMessagePatch(seen=True))
        assert result.saved and result.seen
        assert ns.moved == [f"/domains/{DOMAIN}/users/1001/voicemails/new/vm-1.wav/save"]

    def test_plain_dates_filter(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        box = adapter.retrieve_voicemails(session, UserInfo(user_id=f"1001@{DOMAIN}"),
                                          from_date="2026-11-14", to_date="2026-11-30")
        assert box.messages == []


class TestWebTabToken:
    def test_the_token_names_the_user(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        answer = adapter.custom_method_private(session, f"1001@{DOMAIN}", "external-page-access-token", {})
        claims = jose_jwt.decode(answer["access_token"], SSO_SECRET, algorithms=["HS256"], audience="your-saas")
        assert claims["sub"] == f"1001@{DOMAIN}"
        assert claims["iss"] == "portadialer"
        assert answer["expires_at"]

    def test_other_methods_are_unknown(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        with pytest.raises(WebTritErrorException) as error:
            adapter.custom_method_private(session, f"1001@{DOMAIN}", "something", {})
        assert error.value.status_code == 404
