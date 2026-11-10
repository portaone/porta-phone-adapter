"""An account with an empty SIP password (WT-2196).

PortaBilling leaves `h323_password` out of `get_account_info` when it is empty. User info
used to read it unconditionally and answered 500 on the KeyError; it now answers
403 `sip_credentials_missing`. Sign-in with SIP credentials read it the same way and now
refuses with 401 `incorrect_credentials`.
"""

import os
import sys
import types

import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

from bss.adapters.portaswitch.adapter import PortaSwitchAdapter
from bss.adapters.portaswitch.types import PortaSwitchSignInCredentialsType
from bss.types import SessionInfo, SIPServer, UserInfo
from report_error import WebTritErrorException

ACCOUNT_ID = "380441234567"


def account_info(**extra):
    info = {
        "i_account": 10,
        "i_customer": 20,
        "id": ACCOUNT_ID,
        "login": ACCOUNT_ID,
        "is_active": 1,
        "time_zone_name": "Europe/Kyiv",
        "balance": 0,
        "billing_model": 1,
        "iso_4217": "USD",
    }
    info.update(extra)
    return info


class FakeAccountAPI:
    def __init__(self, info):
        self._info = info

    async def get_account_info(self, access_token):
        return {"account_info": self._info}

    async def get_alias_list(self, access_token):
        return {"alias_list": []}


class FakeAdminAPI:
    def __init__(self, info):
        self._info = info

    async def get_account_info(self, **params):
        return {"account_info": self._info}


def adapter(info):
    subject = object.__new__(PortaSwitchAdapter)
    subject._account_api = FakeAccountAPI(info)
    subject._admin_api = FakeAdminAPI(info)
    subject._sip_server = SIPServer(host="sip.example.com", port=5060)
    subject._settings = types.SimpleNamespace(JANUS_SIP_FORCE_TCP=False)
    subject._portaswitch_settings = types.SimpleNamespace(
        HIDE_BALANCE_IN_USER_INFO=False,
        CRM_HOST_CUSTOM_FIELD=None,
        SIGNIN_CREDENTIALS=PortaSwitchSignInCredentialsType.SIP,
        ALLOWED_ADDONS=[],
    )
    return subject


def retrieve_user(subject):
    session = SessionInfo(user_id="10", access_token="at")
    return subject.retrieve_user(session, UserInfo(user_id="10"))


class TestUserInfo:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("info", [account_info(), account_info(h323_password="")])
    async def test_no_sip_password_is_a_403(self, info):
        with pytest.raises(WebTritErrorException) as error:
            await retrieve_user(adapter(info))

        assert error.value.status_code == 403
        assert error.value.code == "sip_credentials_missing"

    @pytest.mark.asyncio
    async def test_sip_password_is_served(self):
        user = await retrieve_user(adapter(account_info(h323_password="secret")))

        assert user.sip.password == "secret"


class TestSipSignIn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("password", ["", "anything"])
    async def test_no_sip_password_refuses_sign_in(self, password):
        with pytest.raises(WebTritErrorException) as error:
            await adapter(account_info()).authenticate(UserInfo(user_id="N/A", login=ACCOUNT_ID), password)

        assert error.value.status_code == 401
        assert error.value.code == "incorrect_credentials"


class FakeLoginAccountAPI:
    def __init__(self):
        self.logins = []

    async def login(self, login, password=None, token=None):
        self.logins.append((login, password, token))
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}


class FakeTokenlessAdminAPI:
    def __init__(self, version):
        self._version = version

    async def get_version(self):
        return self._version

    async def update_account(self, i_account, **fields):
        raise RuntimeError("api_token cannot be set")


class TestLoginWithoutSelfCarePassword:
    """With no self-care password and no api_token, the login used to raise KeyError."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["MR100", "MR131"])
    async def test_login_is_sent_without_a_password(self, version):
        subject = adapter(account_info(h323_password="secret"))
        subject._admin_api = FakeTokenlessAdminAPI(version)
        subject._account_api = FakeLoginAccountAPI()

        await subject._login_account(account_info(h323_password="secret"))

        assert subject._account_api.logins == [(ACCOUNT_ID, None, None)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version, expected", [("MR100", ("pw", "pw")), ("MR131", ("pw", None))])
    async def test_a_password_is_still_sent(self, version, expected):
        subject = adapter(account_info())
        subject._admin_api = FakeTokenlessAdminAPI(version)
        subject._account_api = FakeLoginAccountAPI()

        await subject._login_account(account_info(password="pw"))

        assert subject._account_api.logins == [(ACCOUNT_ID, *expected)]

    @pytest.mark.asyncio
    async def test_no_self_care_login_is_a_422(self):
        info = account_info(h323_password="secret")
        del info["login"]
        subject = adapter(info)
        subject._admin_api = FakeTokenlessAdminAPI("MR131")
        subject._account_api = FakeLoginAccountAPI()

        with pytest.raises(WebTritErrorException) as error:
            await subject._login_account(info)

        assert error.value.status_code == 422
        assert error.value.code == "self_care_login_missing"
        assert subject._account_api.logins == []
