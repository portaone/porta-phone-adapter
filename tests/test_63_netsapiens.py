"""The NetSapiens adapter against a stubbed NetSapiens API.

The answers below are shaped like the ones the NetSapiens developer sandbox (v45) gave:
devices carry the SIP password, contacts carry no registration state, DIDs live only in
/phonenumbers, CDR user fields are numbers, voicemail sits in the "new" / "save" folders.
"""
import json
import os
import subprocess
import sys
import time

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

import pytest
import jwt

import bss.adapters.netsapiens as netsapiens
from app_config import AppConfig
from bss.types import (Capabilities, ConnectStatus, Direction, SIPRegistrationStatus, UserInfo,
                       ExtendedUserInfo, UserVoicemailMessagePatch)
from report_error import WebTritErrorException

DOMAIN = "northwind.example"
SSO_SECRET = "sso-shared-secret"
# signs the tokens of the fake NetSapiens; the adapter never knows it
NS_SECRET = "netsapiens-own-key"
SECRET_KEY_BASE = "adapter-secret-key-base"


def ns_token(user, token_type="access", lifetime=4 * 3600, domain=DOMAIN):
    """A token shaped like the ones NetSapiens issues (HS256, its own key)"""
    now = int(time.time())
    return jwt.encode({"iss": "ns.example", "aud": "ns", "sub": f"{user}@{domain}", "user": user,
                       "domain": domain, "user_scope": "Basic User", "token_type": token_type,
                       "iat": now, "exp": now + lifetime}, NS_SECRET, algorithm="HS256")

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
                {"phonenumber": "14155550102", "dial-rule-translation-destination-user": "1002"},
                {"phonenumber": "14155550112", "dial-rule-translation-destination-user": "1002"}]
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
    # Mark called Jane, nobody answered and her mailbox took the call after 25 s
    {"call-orig-call-id": "c4", "call-orig-user": 1002, "call-orig-to-user": "1001", "call-orig-request-user": 1001,
     "call-direction": 2, "call-batch-start-datetime": "2026-11-13T10:50:00+00:00",
     "call-start-datetime": "2026-11-13T10:50:25+00:00", "call-answer-datetime": "2026-11-13T10:50:25+00:00",
     "call-talking-duration-seconds": 24, "call-total-duration-seconds": 24, "call-disconnect-reason-text": "Orig: Bye",
     "call-term-to-uri": "VMail", "call-term-match-uri": f"sip:record-vmail@{DOMAIN}", "call-term-user": 1001},
    # Jane called Mark, who transferred her to 1000: the leg after the transfer
    {"call-orig-call-id": "c5", "call-orig-user": 1001, "call-orig-to-user": "X1000", "call-orig-request-user": 1002,
     "call-direction": 1, "call-start-datetime": "2026-11-13T10:40:06+00:00",
     "call-answer-datetime": "2026-11-13T10:40:06+00:00", "call-talking-duration-seconds": 21,
     "call-total-duration-seconds": 21, "call-disconnect-reason-text": "Orig: Bye",
     "call-term-user": 1000, "call-through-user": 1000},
    # Mark called Jane, nobody answered and he hung up during her mailbox greeting
    {"call-orig-call-id": "c6", "call-orig-user": 1002, "call-orig-to-user": "1001", "call-orig-request-user": 1001,
     "call-direction": 2, "call-batch-start-datetime": "2026-11-13T10:30:00+00:00",
     "call-start-datetime": "2026-11-13T10:30:25+00:00", "call-answer-datetime": "2026-11-13T10:30:25+00:00",
     "call-talking-duration-seconds": 4, "call-total-duration-seconds": 4, "call-disconnect-reason-text": "Orig: Bye",
     "call-term-to-uri": "SpeakAccount", "call-term-match-uri": "sip:nms@core1.ns.example", "call-term-user": 1001},
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
        self.whoami = []
        self.forwarded = []
        self.refused_users = set()

    def __call__(self, method, url, timeout=None, headers=None, json=None, **kwargs):
        self.auth.append((url, (headers or {}).get("Authorization")))
        path = url.split("/ns-api/v2", 1)[-1].split("?")[0]
        if path == "/jwt":
            self.jwt_requests.append(json)
            if json["user"] in self.refused_users:
                return FakeResponse({"code": 403, "message": "Permission denied"}, status_code=403)
            return FakeResponse({"token": ns_token(json["user"]), "refresh": ns_token(json["user"], "refresh")})
        if path == "/domains/~/users/~":
            # NetSapiens checks the signature of the token and says whose it is
            token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
            self.whoami.append(token)
            try:
                claims = jwt.decode(token, NS_SECRET, algorithms=["HS256"], audience="ns")
            except jwt.PyJWTError:
                return FakeResponse({"code": 401}, status_code=401)
            if claims["token_type"] != "access":
                return FakeResponse({"code": 401}, status_code=401)
            return FakeResponse({"user": claims["user"], "domain": claims["domain"], "user-scope": "Basic User"})
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
            if method == "PATCH" and path.endswith("/forward"):
                dest = (json or {}).get("voicemail-forward-new-destination")
                if dest == "1099":
                    return FakeResponse({"code": 507, "message": "Insufficient storage for user"}, status_code=507)
                if "/voicemails/save/" not in path:
                    return FakeResponse({"code": 400, "message": "Source file not valid"}, status_code=400)
                self.forwarded.append((path, dest))
                return FakeResponse({"code": 202}, status_code=202)
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


def make_adapter(monkeypatch, oauth=False, sso=True, secret_key_base=SECRET_KEY_BASE):
    client = {"domain": DOMAIN, "api_server": "https://ns.example", "api_key": "domain-key",
              "device_filter": "a", "default_sip_outbound_proxy": "core.ns.example"}
    if oauth:
        client.update(client_id="cid", client_secret="csecret")
    monkeypatch.setenv("NETSAPIENS_CLIENTS", json.dumps([client]))
    if secret_key_base:
        monkeypatch.setenv("SECRET_KEY_BASE", secret_key_base)
    else:
        monkeypatch.delenv("SECRET_KEY_BASE", raising=False)
    if sso:
        monkeypatch.setenv("NETSAPIENS_SSO_SECRET", SSO_SECRET)
        monkeypatch.setenv("NETSAPIENS_SSO_ISSUER", "your-saas")
    else:
        monkeypatch.delenv("NETSAPIENS_SSO_SECRET", raising=False)
    # a new adapter is a restart or another replica: the token cache of the API client is shared
    # by the class, so it starts empty for each one
    monkeypatch.setattr(netsapiens.NetsapiensAPI, "SHARED_TOKENS", None)
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
    # None drops a claim
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, algorithm="HS256")


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

    def test_pbx_and_core_side_features(self, adapter):
        caps = set(adapter.get_capabilities())
        assert {Capabilities.notifications, Capabilities.notifications_push, Capabilities.sip_dialogs,
                Capabilities.sip_mwi, Capabilities.internal_messaging, Capabilities.conference,
                Capabilities.conversation_mute, Capabilities.session_auto_terminate,
                Capabilities.callHistory} <= caps
        # NetSapiens refuses a presence PUBLISH
        assert Capabilities.sip_presence not in caps

    def test_a_feature_can_be_switched_off(self, monkeypatch, ns):
        monkeypatch.setenv("CAPABILITIES_SIP_DIALOGS", "false")
        monkeypatch.setenv("CAPABILITIES_VOICEMAIL", "false")
        caps = set(make_adapter(monkeypatch).get_capabilities())
        assert Capabilities.sip_dialogs not in caps
        # the message waiting subscription goes with the voicemail screen
        assert not {Capabilities.voicemail, Capabilities.sip_mwi} & caps


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
                                      {"aud": None}, {"iss": "not-your-saas"}, {"iss": None},
                                      {"sub": "1001@unknown.example"}])
    def test_a_bad_token_is_refused(self, adapter, over):
        with pytest.raises(WebTritErrorException) as error:
            adapter.autoprovision_session(sso_token(**over))
        assert error.value.status_code == 401

    def test_not_a_jwt_is_refused(self, adapter):
        with pytest.raises(WebTritErrorException) as error:
            adapter.autoprovision_session("not-a-jwt")
        assert error.value.status_code == 401

    def test_the_embedded_sign_in_page_goes_through_signup(self, adapter):
        session = adapter.signup({"sso_token": sso_token()})
        assert session.user_id.root == f"1001@{DOMAIN}"


class TestStatelessSessions:
    """Nothing about a session is stored: the access token is the NetSapiens token, the refresh
    token is signed by the adapter, so a restart or another replica serves the same session."""

    def test_the_app_gets_the_netsapiens_token(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        claims = jwt.decode(session.access_token.root, NS_SECRET, algorithms=["HS256"], audience="ns")
        assert claims["sub"] == f"1001@{DOMAIN}"
        assert session.refresh_token is not None
        # Core refreshes ahead of it: the NetSapiens token lives 4 hours
        assert 4 * 3600 - 60 < session.expires_in <= 4 * 3600

    def test_another_replica_takes_the_session_over(self, monkeypatch, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        other = make_adapter(monkeypatch)

        checked = other.validate_session(session.access_token.root)
        assert checked.user_id.root == f"1001@{DOMAIN}"
        assert ns.whoami == [session.access_token.root]
        # and acts with the app's own token, minting none
        other.retrieve_contacts(checked, UserInfo(user_id=f"1001@{DOMAIN}"))
        assert len(ns.jwt_requests) == 1
        assert (f"https://ns.example/ns-api/v2/domains/{DOMAIN}/users/1001/contacts",
                f"Bearer {session.access_token.root}") in ns.auth

    def test_netsapiens_is_asked_once_for_a_while(self, adapter, ns):
        token = ns_token("1001")
        adapter.validate_session(token)
        adapter.validate_session(token)
        assert ns.whoami == [token]

    @pytest.mark.parametrize("token", ["not-a-jwt", ns_token("1001", "refresh"),
                                       jwt.encode({"sub": f"1001@{DOMAIN}", "domain": DOMAIN,
                                                   "exp": int(time.time()) + 3600}, "forged", algorithm="HS256")])
    def test_a_token_netsapiens_does_not_accept_is_refused(self, adapter, token):
        with pytest.raises(WebTritErrorException) as error:
            adapter.validate_session(token)
        assert error.value.status_code == 401

    def test_an_expired_token_is_refused_without_asking(self, adapter, ns):
        with pytest.raises(WebTritErrorException) as error:
            adapter.validate_session(ns_token("1001", lifetime=-10))
        assert (error.value.status_code, error.value.code) == (401, "access_token_expired")
        assert ns.whoami == []

    def test_a_token_of_another_domain_is_refused(self, adapter):
        with pytest.raises(WebTritErrorException) as error:
            adapter.validate_session(ns_token("1001", domain="other.example"))
        assert error.value.status_code == 401

    def test_another_replica_refreshes(self, monkeypatch, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        fresh = make_adapter(monkeypatch).refresh_session(session.refresh_token.root)
        assert fresh.user_id.root == f"1001@{DOMAIN}"
        # a new delegated token, minted by the replica that never saw the session
        assert ns.jwt_requests == [{"user": "1001", "domain": DOMAIN}] * 2
        assert fresh.refresh_token is not None

    def test_a_refresh_token_of_another_key_is_refused(self, monkeypatch, adapter):
        session = adapter.autoprovision_session(sso_token())
        with pytest.raises(WebTritErrorException) as error:
            make_adapter(monkeypatch, secret_key_base="another-key").refresh_session(session.refresh_token.root)
        assert (error.value.status_code, error.value.code) == (401, "refresh_token_invalid")

    def test_the_netsapiens_refresh_token_is_not_a_refresh_token_here(self, adapter):
        # NetSapiens' own refresh token cannot be checked without an OAuth client
        with pytest.raises(WebTritErrorException) as error:
            adapter.refresh_session(ns_token("1001", "refresh"))
        assert error.value.code == "refresh_token_invalid"

    def test_an_expired_refresh_token(self, monkeypatch, ns):
        monkeypatch.setenv("SESSIONS_REFRESH_EXPIRATION", "0")
        subject = make_adapter(monkeypatch)
        session = subject.autoprovision_session(sso_token())
        time.sleep(1)
        with pytest.raises(WebTritErrorException) as error:
            subject.refresh_session(session.refresh_token.root)
        assert error.value.code == "refresh_token_expired"

    def test_a_user_gone_from_netsapiens_is_not_refreshed(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        ns.refused_users.add("1001")
        with pytest.raises(WebTritErrorException) as error:
            adapter.refresh_session(session.refresh_token.root)
        assert (error.value.status_code, error.value.code) == (401, "refresh_token_invalid")

    def test_without_secret_key_base_there_is_no_refresh(self, monkeypatch, ns):
        subject = make_adapter(monkeypatch, secret_key_base=None)
        session = subject.autoprovision_session(sso_token())
        assert session.refresh_token is None
        with pytest.raises(WebTritErrorException) as error:
            subject.refresh_session("anything")
        assert error.value.status_code == 401

    def test_sign_out(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        assert adapter.close_session(session.access_token.root) is True
        # the next request asks NetSapiens again
        adapter.validate_session(session.access_token.root)
        assert ns.whoami == [session.access_token.root]


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

    def test_dids_are_the_numbers_of_a_contact(self, adapter):
        # a colleague calling from their DID is found among the contacts by it
        session = adapter.autoprovision_session(sso_token())
        contacts = adapter.retrieve_contacts(session, UserInfo(user_id=f"1001@{DOMAIN}"))

        assert {c.numbers.ext: (c.numbers.main, c.numbers.additional) for c in contacts} == {
            "1000": ("1000", []), "1002": ("+14155550102", ["+14155550112"])}
        items, _ = adapter.retrieve_contacts_v2(session, UserInfo(user_id=f"1001@{DOMAIN}"),
                                                phone_numbers=["14155550112"])
        assert [c.numbers.ext for c in items] == ["1002"]

    @pytest.mark.parametrize("kwargs, expected", [
        ({"phone_numbers": ["1002"]}, ["1002"]),
        ({"search": "mark"}, ["1002"]),
        ({"search": "100"}, ["1000", "1002"]),
        ({"items_per_page": 1, "page": 2}, ["1002"]),
        ({"phone_numbers": ["+14155550102"]}, ["1002"]),
    ])
    def test_v2_filters(self, adapter, kwargs, expected):
        session = adapter.autoprovision_session(sso_token())
        items, total = adapter.retrieve_contacts_v2(session, UserInfo(user_id=f"1001@{DOMAIN}"), **kwargs)
        assert [c.numbers.ext for c in items] == expected


    def test_one_contact_by_its_id(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        contact = adapter.retrieve_contact_by_user_id(session, UserInfo(user_id=f"1001@{DOMAIN}"), f"1002@{DOMAIN}")
        assert contact.numbers.ext == "1002"
        assert contact.sip_status == SIPRegistrationStatus.registered

    def test_an_unknown_contact_is_not_found(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        with pytest.raises(WebTritErrorException) as error:
            adapter.retrieve_contact_by_user_id(session, UserInfo(user_id=f"1001@{DOMAIN}"), f"9999@{DOMAIN}")
        assert error.value.status_code == 404


class TestCallHistory:
    def test_direction_status_and_duration_from_janes_side(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        items, total = adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"))

        assert total == 6
        by_id = {i.call_id: i for i in items}
        assert (by_id["c1"].direction, by_id["c1"].status, by_id["c1"].duration) == (
            Direction.outgoing, ConnectStatus.accepted, 16)
        assert (by_id["c1"].caller, by_id["c1"].callee) == ("1001", "1002")
        assert (by_id["c2"].direction, by_id["c2"].status) == (Direction.incoming, ConnectStatus.missed)
        assert (by_id["c3"].direction, by_id["c3"].status) == (Direction.outgoing, ConnectStatus.declined)

    @pytest.mark.parametrize("call_id, rang_from", [
        ("c4", "2026-11-13T10:50:00+00:00"),  # a message was left
        ("c6", "2026-11-13T10:30:00+00:00"),  # the caller hung up during the greeting
    ])
    def test_a_call_her_mailbox_took_is_missed(self, adapter, call_id, rang_from):
        session = adapter.autoprovision_session(sso_token())
        items, _ = adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"))
        call = {i.call_id: i for i in items}[call_id]
        assert (call.direction, call.status, call.duration) == (Direction.incoming, ConnectStatus.missed, 0)
        # it rang from the start, not from when the mailbox picked up
        assert call.connect_time.isoformat() == rang_from

    def test_the_mailbox_of_the_called_answers_for_the_caller(self, adapter):
        # Mark's side of the same call: he did talk to the mailbox
        session = adapter.autoprovision_session(sso_token(sub=f"1002@{DOMAIN}"))
        items, _ = adapter.retrieve_calls(session, UserInfo(user_id=f"1002@{DOMAIN}"))
        c4 = {i.call_id: i for i in items}["c4"]
        assert (c4.direction, c4.status, c4.duration) == (Direction.outgoing, ConnectStatus.accepted, 24)

    def test_the_transfer_mark_is_not_part_of_the_number(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        items, _ = adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"))
        c5 = {i.call_id: i for i in items}["c5"]
        assert (c5.caller, c5.callee, c5.status) == ("1001", "1000", ConnectStatus.accepted)

    def test_newest_first_with_a_real_total(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        items, total = adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"), page=1, items_per_page=2)
        assert [i.call_id for i in items] == ["c2", "c1"]
        assert total == 6

    def test_recorded_calls_carry_a_recording_id(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        items, _ = adapter.retrieve_calls(session, UserInfo(user_id=f"1001@{DOMAIN}"))
        assert {i.call_id: i.recording_id for i in items} == {"c1": "c1", "c2": None, "c3": None, "c4": None, "c5": None, "c6": None}

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
        # "57.86 KB": the API counts whole KB, which Core holds against its forwarding limit
        assert msg.size == 58

        result = adapter.patch_voicemail_message(session, "vm-1.wav", UserVoicemailMessagePatch(seen=True))
        assert result.saved and result.seen
        assert ns.moved == [f"/domains/{DOMAIN}/users/1001/voicemails/new/vm-1.wav/save"]

    def test_the_recording_downloads_as_wav(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        content_type, body = adapter.retrieve_voicemail_message_attachment(session, "vm-1.wav", None)
        assert content_type == "audio/wav"
        assert b"".join(body) == b"RIFF....WAVE"

    def test_mp3_needs_sox(self, adapter):
        # Core asks for mp3 when it keeps a copy of a forwarded message
        session = adapter.autoprovision_session(sso_token())
        with pytest.raises(WebTritErrorException) as error:
            adapter.retrieve_voicemail_message_attachment(session, "vm-1.wav", "mp3")
        assert error.value.status_code == 422

    def test_mp3_is_converted_with_sox(self, adapter, monkeypatch):
        ran = []

        def sox(cmd, input=None, **kw):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"ID3-mp3:" + input)

        monkeypatch.setattr(netsapiens.shutil, "which", lambda name: "/usr/bin/sox")
        monkeypatch.setattr(netsapiens.subprocess, "run", sox)
        session = adapter.autoprovision_session(sso_token())
        content_type, body = adapter.retrieve_voicemail_message_attachment(session, "vm-1.wav", "MP3")
        assert content_type == "audio/mpeg"
        assert b"".join(body) == b"ID3-mp3:RIFF....WAVE"
        assert ran[0][0] == "sox" and ran[0][ran[0].index("-t", 3) + 1] == "mp3"

    def test_a_heard_message_cannot_go_back_to_unheard(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        VOICEMAIL["save"].append({"filename": "vm-0.wav", "file-duration-seconds": 5, "file-size-kilobytes": "10 KB",
                                  "created-datetime": "2026-11-12T10:00:00+00:00", "voicemail-from-user": "1002",
                                  "file-access-url": "https://ns.example/audio/vm-0.wav"})
        try:
            with pytest.raises(WebTritErrorException) as error:
                adapter.patch_voicemail_message(session, "vm-0.wav", UserVoicemailMessagePatch(seen=False))
            assert (error.value.status_code, error.value.code) == (422, "unsupported_operation")
            assert ns.moved == []
        finally:
            VOICEMAIL["save"].clear()

    def test_forward_is_advertised_as_netsapiens_own(self, adapter):
        caps = set(adapter.get_capabilities())
        assert {Capabilities.voicemail_forward, Capabilities.voicemail_forward_adaptee} <= caps

    def test_an_unheard_message_is_saved_then_forwarded(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        adapter.forward_voicemail_message(session, "vm-1.wav", f"1002@{DOMAIN}")
        assert ns.moved == [f"/domains/{DOMAIN}/users/1001/voicemails/new/vm-1.wav/save"]
        assert ns.forwarded == [(f"/domains/{DOMAIN}/users/1001/voicemails/save/vm-1.wav/forward", "1002")]

    def test_a_recipient_of_another_domain_is_not_found(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        with pytest.raises(WebTritErrorException) as error:
            adapter.forward_voicemail_message(session, "vm-1.wav", "1002@other.example")
        assert (error.value.status_code, error.value.code) == (404, "recipient_not_found")
        assert ns.forwarded == [] and ns.moved == []

    def test_a_mailbox_that_cannot_take_it(self, adapter, ns):
        session = adapter.autoprovision_session(sso_token())
        with pytest.raises(WebTritErrorException) as error:
            adapter.forward_voicemail_message(session, "vm-1.wav", f"1099@{DOMAIN}")
        assert (error.value.status_code, error.value.code) == (422, "recipient_mailbox_full")

    def test_plain_dates_filter(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        box = adapter.retrieve_voicemails(session, UserInfo(user_id=f"1001@{DOMAIN}"),
                                          from_date="2026-11-14", to_date="2026-11-30")
        assert box.messages == []


class TestWebTabToken:
    def test_the_token_names_the_user(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        answer = adapter.custom_method_private(session, f"1001@{DOMAIN}", "external-page-access-token", {})
        claims = jwt.decode(answer["access_token"], SSO_SECRET, algorithms=["HS256"], audience="your-saas")
        assert claims["sub"] == f"1001@{DOMAIN}"
        assert claims["iss"] == "portadialer"
        assert answer["expires_at"]

    def test_other_methods_are_unknown(self, adapter):
        session = adapter.autoprovision_session(sso_token())
        with pytest.raises(WebTritErrorException) as error:
            adapter.custom_method_private(session, f"1001@{DOMAIN}", "something", {})
        assert error.value.status_code == 404
