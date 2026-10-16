"""The FreePBX adapter against a stubbed FreePBX API (WT-1931).

The adapter was never ported to the refactored HTTPAPIConnectorWithLogin and did not
even import. These tests drive it the way main.py does - instantiate, sign in, read the
user and the contacts - with `requests.request` answering like FreePBX's OAuth token
endpoint and GraphQL API, including the nulls GraphQL returns for unset fields.
"""
import os
import sys
from urllib.parse import parse_qs

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

import pytest

import bss.adapters.freepbx as freepbx
import bss.http_api as http_api
from app_config import AppConfig
from bss.dbs import TiedKeyValue
from bss.http_api import OAuthSessionData
from bss.sessions import SessionStorage
from bss.types import Capabilities, ExtendedUserInfo, UserInfo
from report_error import WebTritErrorException

EXTENSIONS = {
    "1001": {"id": "1", "extensionId": "1001",
             "user": {"name": "John Smith", "password": "", "extPassword": "sip-secret",
                      "outboundCid": '"John" <15551001>'}},
    # no name, no outbound CID, no voicemail
    "1002": {"id": "2", "extensionId": "1002",
             "user": {"name": "", "password": "", "extPassword": "other", "outboundCid": None}},
}
VOICEMAIL = {
    "1001": {"status": True, "name": "John Smith", "password": "1234", "email": ""},
    "1002": None,
}


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.text = str(body)

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeFreePBX:
    """Answers the token endpoint and the three GraphQL queries the adapter sends."""

    def __init__(self, token_response=None):
        self.token_response = token_response or {"access_token": "AT", "expires_in": 3600}
        self.token_requests = []
        self.graphql_auth = []

    def __call__(self, method, url, timeout=None, headers=None, data=None, json=None, **kwargs):
        if url.endswith("/admin/api/api/token"):
            self.token_requests.append(parse_qs(data) if isinstance(data, str) else data)
            return FakeResponse(self.token_response)

        self.graphql_auth.append((headers or {}).get("Authorization"))
        query = json["query"]
        if "fetchAllExtensions" in query:
            return FakeResponse({"data": {"fetchAllExtensions": {
                "status": True, "totalCount": len(EXTENSIONS), "extension": list(EXTENSIONS.values())}}})
        ext_id = query.split("extensionId:")[1].split(")")[0].strip()
        if "fetchExtension" in query:
            if ext_id not in EXTENSIONS:
                return FakeResponse({"errors": [{"message": "Extension does not exists"}], "data": None})
            return FakeResponse({"data": {"fetchExtension": dict(EXTENSIONS[ext_id])}})
        return FakeResponse({"data": {"fetchVoiceMail": VOICEMAIL.get(ext_id)}})


@pytest.fixture
def fake_pbx(monkeypatch):
    pbx = FakeFreePBX()
    monkeypatch.setattr(http_api.requests, "request", pbx)
    return pbx


@pytest.fixture
def adapter(monkeypatch, fake_pbx):
    monkeypatch.setattr(freepbx, "configure_session_storage",
                        lambda config: SessionStorage(session_db=TiedKeyValue()))
    subject = freepbx.FreePBXAdapter(AppConfig({"FreePBX": {
        "API_Server": "http://pbx", "API_User": "client", "API_Secret": "secret"}}))
    subject.initialize()
    return subject


def sign_in(adapter, login="1001", password="sip-secret"):
    # what main.py hands over: the user_id is not known before sign-in
    return adapter.authenticate(ExtendedUserInfo(user_id="N/A", login=login, tenant_id="default"), password)


class TestStartup:
    def test_instantiates(self, adapter):
        assert adapter.name() == "FreePBX adapter"

    def test_capabilities_follow_the_config_switches(self, adapter):
        assert {Capabilities.passwordSignin, Capabilities.extensions,
                Capabilities.direct_presence} <= set(adapter.get_capabilities())

        adapter.config = AppConfig({"Capabilities": {"DIRECT_PRESENCE": "0"}})
        assert Capabilities.direct_presence not in adapter.calculate_capabilities()


class TestSignIn:
    def test_the_sip_password_signs_in(self, adapter, fake_pbx):
        session = sign_in(adapter)

        assert session.user_id.root == "1001"
        assert fake_pbx.token_requests[0]["grant_type"] == "client_credentials"
        assert set(fake_pbx.graphql_auth) == {"Bearer AT"}

    def test_the_voicemail_pin_does_not(self, adapter):
        with pytest.raises(WebTritErrorException) as error:
            sign_in(adapter, password="1234")
        assert error.value.status_code == 401

    def test_an_extension_without_voicemail_signs_in(self, adapter):
        assert sign_in(adapter, login="1002", password="other")

    def test_an_empty_password_is_refused(self, adapter):
        EXTENSIONS["1003"] = {"id": "3", "extensionId": "1003", "user": {"name": "X", "extPassword": ""}}
        try:
            with pytest.raises(WebTritErrorException):
                sign_in(adapter, login="1003", password="")
        finally:
            del EXTENSIONS["1003"]

    def test_an_unknown_extension_is_refused(self, adapter):
        with pytest.raises(WebTritErrorException) as error:
            sign_in(adapter, login="9999")
        assert error.value.status_code == 401

    def test_the_token_is_reused(self, adapter, fake_pbx):
        sign_in(adapter)
        sign_in(adapter)
        assert len(fake_pbx.token_requests) == 1

    def test_a_token_response_without_refresh_token_or_expiry(self, adapter, fake_pbx):
        fake_pbx.token_response = {"access_token": "AT"}
        assert sign_in(adapter)


class TestTokenModel:
    def test_absent_fields_may_be_passed_as_none(self):
        session = OAuthSessionData(access_token="a", access_token_expires_at=None, refresh_token=None)
        assert session.refresh_token is None


class TestUserInfo:
    def test_empty_voicemail_email_is_left_out(self, adapter):
        session = sign_in(adapter)
        user = adapter.retrieve_user(session, UserInfo(user_id="1001"))

        assert user.email is None
        assert user.numbers.main == "15551001"
        assert user.numbers.sms == []
        assert user.sip.password == "sip-secret"

    def test_extension_without_voicemail_or_name(self, adapter):
        user = adapter.retrieve_user(None, UserInfo(user_id="1002"))

        assert user.email is None
        assert user.numbers.main == "1002"
        assert user.sip.display_name == "Ext 1002"


class TestVoicemailEmail:
    @pytest.mark.parametrize("value, expected", [
        ("", None),
        (None, None),
        ("john@example.com", "john@example.com"),
        ("john@example.com,boss@example.com", "john@example.com"),
        ("john@example.com|boss@example.com", "john@example.com"),
        ("not-an-address", None),
    ])
    def test_value(self, value, expected):
        assert freepbx.FreePBXAdapter.voicemail_email({"email": value}) == expected


class TestRefresh:
    def test_a_refreshed_token_is_kept(self, adapter, fake_pbx):
        # expires within REFRESH_TOKEN_IN_ADVANCE, so the first call refreshes it
        fake_pbx.token_response = {"access_token": "AT", "expires_in": 60, "refresh_token": "RT"}
        sign_in(adapter)
        fake_pbx.token_response = {"access_token": "AT2", "expires_in": 3600, "refresh_token": "RT2"}
        sign_in(adapter)
        requests_after_refresh = len(fake_pbx.token_requests)
        sign_in(adapter)
        sign_in(adapter)

        assert fake_pbx.token_requests[-1]["grant_type"] == "refresh_token"
        assert len(fake_pbx.token_requests) == requests_after_refresh
        assert fake_pbx.graphql_auth[-1] == "Bearer AT2"


class TestContacts:
    def test_v1_lists_the_other_extensions(self, adapter):
        contacts = adapter.retrieve_contacts(None, UserInfo(user_id="1001"))

        assert [c.numbers.ext for c in contacts] == ["1002"]
        assert contacts[0].user_id.root == "1002"

    def test_v2_paginates(self, adapter):
        contacts, total = adapter.retrieve_contacts_v2(None, UserInfo(user_id="9999"),
                                                       page=2, items_per_page=1)
        assert total == 2
        assert [c.numbers.ext for c in contacts] == ["1002"]

    def test_v2_searches(self, adapter):
        contacts, total = adapter.retrieve_contacts_v2(None, UserInfo(user_id="9999"), search="smi")
        assert total == 1
        assert contacts[0].numbers.ext == "1001"

    def test_v2_looks_up_phone_numbers(self, adapter):
        contacts, total = adapter.retrieve_contacts_v2(None, UserInfo(user_id="9999"),
                                                       phone_numbers=["+15551001"])
        assert [c.numbers.ext for c in contacts] == ["1001"]

    def test_by_user_id(self, adapter):
        assert adapter.retrieve_contact_by_user_id(None, UserInfo(user_id="1001"), "1002").numbers.ext == "1002"

        with pytest.raises(WebTritErrorException) as error:
            adapter.retrieve_contact_by_user_id(None, UserInfo(user_id="1001"), "9999")
        assert error.value.status_code == 404
