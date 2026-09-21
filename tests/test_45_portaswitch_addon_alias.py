"""ALLOWED_ADDONS on the OTP sign-in path (WT-1926).

Add-ons are assigned to the master account: an alias row carries none of its own, and
`Account/get_account_info(id=<alias>)` answers with that alias row. `authenticate` has
resolved the alias to its master since alias login was added, but `generate_otp` used to
check the add-ons of the alias row itself - and `_check_allowed_addons` skipped any row
with `i_master_account`, so an OTP sign-in by alias passed the gate unconditionally.

This file pins both halves: `generate_otp` resolves the master before the check, and the
check no longer waves an alias row through.
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
from bss.types import UserInfo
from report_error import WebTritErrorException

ALLOWED_ADDON = "WebTrit Mobile"
MASTER_I_ACCOUNT = 102398
MASTER_ID = "555020"
ALIAS_ID = "555021"


def master(addons=(ALLOWED_ADDON,)):
    return {
        "i_account": MASTER_I_ACCOUNT,
        "id": MASTER_ID,
        "login": MASTER_ID,
        "assigned_addons": [{"name": name} for name in addons],
    }


def alias():
    """An alias row as PortaBilling returns it: no add-ons, pointing at its master."""
    return {"i_account": 102399, "id": ALIAS_ID, "i_master_account": MASTER_I_ACCOUNT}


class FakeAdminAPI:
    """The admin realm, answering account lookups out of a fixed table."""

    def __init__(self, accounts):
        self._accounts = accounts
        self.lookups = []
        self.created_otps = []

    async def get_account_info(self, **params):
        self.lookups.append(params)
        key = params.get("id", params.get("i_account"))
        return {"account_info": self._accounts.get(str(key))}

    async def create_otp(self, i_account, delivery_channel):
        self.created_otps.append((i_account, delivery_channel))
        return {"success": 1}

    def current_access_token(self):
        return None

    async def get_env_info(self):
        return {"email": "pbx@example.com"}


class FakeOTPStorage:
    def __init__(self):
        self.stored = []

    def store(self, otp_id, i_account, user_id, bss_token=None):
        self.stored.append((otp_id, i_account, user_id, bss_token))


def adapter(accounts, allowed_addons=(ALLOWED_ADDON,)):
    subject = object.__new__(PortaSwitchAdapter)
    subject._admin_api = FakeAdminAPI(accounts)
    subject._otp_storage = FakeOTPStorage()
    subject._portaswitch_settings = types.SimpleNamespace(
        ALLOWED_ADDONS=list(allowed_addons),
        ADMIN_API_TOKEN="token",
    )
    return subject


# --------------------------------------------------------------------------- #
# generate_otp resolves the alias before checking add-ons
# --------------------------------------------------------------------------- #


class TestGenerateOTPAddonCheck:
    @pytest.mark.asyncio
    async def test_alias_is_rejected_when_the_master_lacks_the_addon(self):
        # The ticket's case: signing in by alias number, master without the add-on.
        subject = adapter({ALIAS_ID: alias(), str(MASTER_I_ACCOUNT): master(addons=())})

        with pytest.raises(WebTritErrorException) as error:
            await subject.generate_otp(UserInfo(user_id=ALIAS_ID))

        assert error.value.status_code == 403
        assert error.value.code == "addon_required"
        # No OTP may leave the switch for an account that is not allowed in.
        assert subject._admin_api.created_otps == []
        assert subject._otp_storage.stored == []

    @pytest.mark.asyncio
    async def test_alias_is_let_in_when_the_master_has_the_addon(self):
        subject = adapter({ALIAS_ID: alias(), str(MASTER_I_ACCOUNT): master()})

        response = await subject.generate_otp(UserInfo(user_id=ALIAS_ID))

        assert subject._admin_api.created_otps == [(MASTER_I_ACCOUNT, PortaSwitchAdapter.OTP_DELIVERY_CHANNEL)]
        # The OTP is still bound to the master account, and the alias stays the user_ref.
        assert subject._otp_storage.stored == [(response.otp_id.root, MASTER_I_ACCOUNT, ALIAS_ID, None)]

    @pytest.mark.asyncio
    async def test_master_is_checked_without_a_second_lookup(self):
        subject = adapter({MASTER_ID: master()})

        await subject.generate_otp(UserInfo(user_id=MASTER_ID))

        assert subject._admin_api.lookups == [{"id": MASTER_ID}]
        assert subject._admin_api.created_otps == [(MASTER_I_ACCOUNT, PortaSwitchAdapter.OTP_DELIVERY_CHANNEL)]

    @pytest.mark.asyncio
    async def test_master_without_the_addon_is_rejected(self):
        subject = adapter({MASTER_ID: master(addons=("Other add-on",))})

        with pytest.raises(WebTritErrorException) as error:
            await subject.generate_otp(UserInfo(user_id=MASTER_ID))

        assert error.value.status_code == 403
        assert error.value.code == "addon_required"

    @pytest.mark.asyncio
    async def test_alias_whose_master_vanished_is_not_found(self):
        subject = adapter({ALIAS_ID: alias()})

        with pytest.raises(WebTritErrorException) as error:
            await subject.generate_otp(UserInfo(user_id=ALIAS_ID))

        assert error.value.status_code == 404
        assert subject._admin_api.created_otps == []

    @pytest.mark.asyncio
    async def test_alias_still_works_with_the_gate_switched_off(self):
        # No ALLOWED_ADDONS configured: the master is still the account the OTP is for.
        subject = adapter(
            {ALIAS_ID: alias(), str(MASTER_I_ACCOUNT): master(addons=())},
            allowed_addons=(),
        )

        await subject.generate_otp(UserInfo(user_id=ALIAS_ID))

        assert subject._admin_api.created_otps == [(MASTER_I_ACCOUNT, PortaSwitchAdapter.OTP_DELIVERY_CHANNEL)]


# --------------------------------------------------------------------------- #
# The check itself no longer skips an alias row
# --------------------------------------------------------------------------- #


class TestCheckAllowedAddons:
    def test_an_alias_row_is_not_waved_through(self):
        subject = adapter({})

        with pytest.raises(WebTritErrorException) as error:
            subject._check_allowed_addons(alias())

        assert error.value.code == "addon_required"

    def test_the_master_addon_list_is_honoured(self):
        subject = adapter({})

        # One of the allowed names among several assigned add-ons is enough.
        assert subject._check_allowed_addons(master(addons=("Other add-on", ALLOWED_ADDON))) is None

    def test_an_unrelated_addon_is_not_enough(self):
        subject = adapter({})

        with pytest.raises(WebTritErrorException) as error:
            subject._check_allowed_addons(master(addons=("Other add-on",)))

        assert error.value.code == "addon_required"
