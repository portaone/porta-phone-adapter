"""The access token lifetime Core is told about (WT-1789).

Core refreshes every active session ahead of `expires_in`, so each path that hands it a
session has to report one when it can: PortaBilling's own value on sign-in and refresh, the
JWT `exp` when the client brings PortaBilling tokens itself, and the configured lifetime for
sessions this adapter mints.
"""

import os
import sys
import time

import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

# PortaSwitchSettings is instantiated at import time and its URL/credential fields are
# mandatory; supply throwaway values before importing the adapter.
os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

from jose import jwt

from bss.adapters.portaswitch.api.account import AccountAPI
from bss.models import SessionResponse, SupportedEnum
from bss.sessions import SessionStorage
from bss.types import UserInfo


def token(claims):
    return jwt.encode(claims, "secret", algorithm="HS256")


class TestJwtExpiry:
    def test_seconds_left_until_exp(self):
        assert AccountAPI.access_token_expires_in(token({"exp": int(time.time()) + 2100})) in (2099, 2100)

    def test_an_expired_token_reports_zero(self):
        assert AccountAPI.access_token_expires_in(token({"exp": int(time.time()) - 60})) == 0

    def test_no_exp_claim_is_unknown(self):
        assert AccountAPI.access_token_expires_in(token({"i_account": 1})) is None

    def test_not_a_jwt_is_unknown(self):
        assert AccountAPI.access_token_expires_in("not-a-jwt") is None


class TestMintedSessions:
    def test_carry_the_configured_lifetime(self):
        storage = SessionStorage(session_db={})
        session = storage.create_session(UserInfo(user_id="1"))
        body = SessionResponse.model_validate(session.model_dump()).model_dump(mode="json")

        assert body["expires_in"] == storage.SESSION_EXPIRATION * 3600


def test_capability_is_named_for_core():
    assert SupportedEnum.session_auto_terminate.value == "sessionAutoTerminate"
