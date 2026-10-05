"""Error codes of a failed OTP verification (WT-2051).

The app picks its message by the error `code` and falls back to a generic "server
failure" when there is none, so both failures of `validate_otp` must carry the code
Core's otp-verify contract lists for them under 401.
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

from bss.adapters.portaswitch.adapter import PortaSwitchAdapter
from bss.models import OtpId
from bss.types import OTPVerifyRequest
from report_error import WebTritErrorException

OTP_ID = "otp-1"
I_ACCOUNT = 102398


class FakeAdminAPI:
    def __init__(self, success):
        self._success = success
        self.verified = []

    async def verify_otp(self, otp_token, bss_token=None):
        self.verified.append(otp_token)
        return {"success": self._success}


class FakeOTPStorage:
    def __init__(self, entries):
        self._entries = entries

    def retrieve(self, otp_id):
        return self._entries.get(otp_id, (None, None, None))

    def delete(self, otp_id):
        self._entries.pop(otp_id, None)


def adapter(entries, success=0):
    subject = object.__new__(PortaSwitchAdapter)
    subject._admin_api = FakeAdminAPI(success)
    subject._otp_storage = FakeOTPStorage(entries)
    subject._otp_settings = types.SimpleNamespace(IGNORE_ACCOUNTS=[])
    subject._portaswitch_settings = types.SimpleNamespace(ADMIN_API_TOKEN="token")
    return subject


def request(code="00000000"):
    return OTPVerifyRequest(otp_id=OtpId(OTP_ID), code=code)


class TestValidateOTPErrors:
    @pytest.mark.asyncio
    async def test_wrong_code_is_incorrect_otp_code(self):
        subject = adapter({OTP_ID: (I_ACCOUNT, "555002", None)}, success=0)

        with pytest.raises(WebTritErrorException) as error:
            await subject.validate_otp(request("00000000"))

        assert error.value.status_code == 401
        assert error.value.code == "incorrect_otp_code"
        assert subject._admin_api.verified == ["00000000"]

    @pytest.mark.asyncio
    async def test_wrong_code_keeps_the_otp_for_another_attempt(self):
        entries = {OTP_ID: (I_ACCOUNT, "555002", None)}
        subject = adapter(entries, success=0)

        with pytest.raises(WebTritErrorException):
            await subject.validate_otp(request())

        assert OTP_ID in entries

    @pytest.mark.asyncio
    async def test_missing_storage_entry_is_otp_expired(self):
        subject = adapter({}, success=1)

        with pytest.raises(WebTritErrorException) as error:
            await subject.validate_otp(request())

        assert error.value.status_code == 401
        assert error.value.code == "otp_expired"
        # Nothing to verify against: the switch is not asked.
        assert subject._admin_api.verified == []

    def test_wrong_code_body_carries_the_code(self):
        from bss.adapters.portaswitch.exceptions import incorrect_otp_code_error

        body = incorrect_otp_code_error("00000000").response().body

        assert b'"code":"incorrect_otp_code"' in body
