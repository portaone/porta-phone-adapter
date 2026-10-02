"""The OTP notification email on the sign-in path (WT-1663).

`generate_otp` used to call `Env/get_env_info` after `create_otp` had already sent the
code. On a slow switch that last call could time out or overrun the request deadline,
the client got an error and retried, and the user received a second code.

This file pins the fix: the email is read before the code is sent, it is cached, and a
failed lookup only drops `delivery_from` instead of failing the request.
"""

import os
import sys
import types

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

from bss.adapters.portaswitch import adapter as adapter_module
from bss.adapters.portaswitch.adapter import PortaSwitchAdapter
from bss.types import UserInfo
from report_error import WebTritErrorException

ACCOUNT_ID = "555020"
I_ACCOUNT = 102398
ENV_EMAIL = "pbx@example.com"


class FakeAdminAPI:
    """The admin realm, recording the order of the calls that matter here."""

    def __init__(self, env_error=None):
        self.calls = []
        self._env_error = env_error

    async def get_account_info(self, **params):
        self.calls.append("get_account_info")
        return {"account_info": {"i_account": I_ACCOUNT, "id": ACCOUNT_ID}}

    async def create_otp(self, i_account, delivery_channel):
        self.calls.append("create_otp")
        return {"success": 1}

    def current_access_token(self):
        return None

    async def get_env_info(self):
        self.calls.append("get_env_info")
        if self._env_error is not None:
            raise self._env_error
        return {"email": ENV_EMAIL}


class FakeOTPStorage:
    def store(self, otp_id, i_account, user_id, bss_token=None):
        pass


def adapter(env_error=None):
    subject = object.__new__(PortaSwitchAdapter)
    subject._admin_api = FakeAdminAPI(env_error)
    subject._otp_storage = FakeOTPStorage()
    subject._init_env_email_cache_state()
    subject._portaswitch_settings = types.SimpleNamespace(ALLOWED_ADDONS=[], ADMIN_API_TOKEN="token")
    return subject


class TestGenerateOtpEnvEmail:
    @pytest.mark.asyncio
    async def test_email_is_read_before_the_code_is_sent(self):
        subject = adapter()

        response = await subject.generate_otp(UserInfo(user_id=ACCOUNT_ID))

        calls = subject._admin_api.calls
        assert response.delivery_from == ENV_EMAIL
        assert calls.index("get_env_info") < calls.index("create_otp")
        assert calls[-1] == "create_otp"

    @pytest.mark.asyncio
    async def test_email_is_cached_across_otp_requests(self):
        subject = adapter()

        await subject.generate_otp(UserInfo(user_id=ACCOUNT_ID))
        response = await subject.generate_otp(UserInfo(user_id=ACCOUNT_ID))

        assert response.delivery_from == ENV_EMAIL
        assert subject._admin_api.calls.count("get_env_info") == 1

    @pytest.mark.asyncio
    async def test_expired_cache_reads_the_email_again(self, monkeypatch):
        subject = adapter()
        now = [1000.0]
        monkeypatch.setattr(adapter_module.time, "monotonic", lambda: now[0])

        await subject.generate_otp(UserInfo(user_id=ACCOUNT_ID))
        now[0] += adapter_module.ENV_EMAIL_CACHE_TTL
        await subject.generate_otp(UserInfo(user_id=ACCOUNT_ID))

        assert subject._admin_api.calls.count("get_env_info") == 2

    @pytest.mark.parametrize("error", [
        WebTritErrorException(status_code=500, error_message="Env/get_env_info timed out"),
        TimeoutError("read timeout"),
    ])
    @pytest.mark.asyncio
    async def test_failed_lookup_still_sends_the_code(self, error):
        subject = adapter(env_error=error)

        response = await subject.generate_otp(UserInfo(user_id=ACCOUNT_ID))

        assert response.delivery_from is None
        assert "create_otp" in subject._admin_api.calls

    @pytest.mark.asyncio
    async def test_failed_lookup_is_not_cached(self):
        subject = adapter(env_error=TimeoutError("read timeout"))
        await subject.generate_otp(UserInfo(user_id=ACCOUNT_ID))

        subject._admin_api._env_error = None
        response = await subject.generate_otp(UserInfo(user_id=ACCOUNT_ID))

        assert response.delivery_from == ENV_EMAIL
        assert subject._admin_api.calls.count("get_env_info") == 2
