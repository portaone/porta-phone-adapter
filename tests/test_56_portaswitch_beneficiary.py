"""Sign-in of a beneficiary account, one that has a sponsor (WT-2209).

PortaBilling fills `i_master_account` both for an alias, where it points at the master
account, and for a beneficiary (`billing_model` 4), where it points at the sponsor. Only
the alias is resolved to its master. A beneficiary signs in with its own credentials and
the session and OTP are its own. Its row already carries the add-ons it inherits from the
sponsor, as seen on a live switch, so ALLOWED_ADDONS is checked on that row and the
sponsor is never looked up. Its device limits are custom fields of its own, not inherited,
so they are read from the beneficiary too.
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
from bss.adapters.portaswitch.config import PortaSwitchSettings
from bss.types import UserInfo
from report_error import WebTritErrorException

ALLOWED_ADDON = "WebTrit Mobile"
SPONSOR_I_ACCOUNT = 160
SPONSOR_ID = "551131640000"
BENEFICIARY_I_ACCOUNT = 161
BENEFICIARY_ID = "551131648398"
PASSWORD = "beneficiary-secret"


def sponsor():
    """The sponsor: a credit account with no web password and, here, no add-ons, so a
    gate that read it instead of the beneficiary would refuse the sign-in."""
    return {
        "i_account": SPONSOR_I_ACCOUNT,
        "i_customer": 7001,
        "id": SPONSOR_ID,
        "login": SPONSOR_ID,
        "billing_model": 1,
        "assigned_addons": [],
    }


def beneficiary(addons=(ALLOWED_ADDON,)):
    """A beneficiary row: own credentials, the inherited add-ons, i_master_account is the sponsor."""
    return {
        "i_account": BENEFICIARY_I_ACCOUNT,
        "i_customer": 7001,
        "id": BENEFICIARY_ID,
        "login": BENEFICIARY_ID,
        "password": PASSWORD,
        "billing_model": 4,
        "i_master_account": SPONSOR_I_ACCOUNT,
        "assigned_addons": [{"name": name} for name in addons],
    }


class FakeAdminAPI:
    def __init__(self, accounts, account_fields=None):
        self._accounts = accounts
        self._account_fields = account_fields or {}
        self.lookups = []
        self.created_otps = []
        self.account_field_reads = []

    async def get_account_info(self, **params):
        self.lookups.append(params)
        key = params.get("id", params.get("login", params.get("i_account")))
        return {"account_info": self._accounts.get(str(key))}

    async def get_account_custom_fields_values(self, i_account):
        self.account_field_reads.append(i_account)
        values = self._account_fields.get(i_account, {})
        return {"custom_fields_values": [{"name": name, "db_value": value} for name, value in values.items()]}

    async def get_customer_custom_fields_values(self, i_customer):
        return {"custom_fields_values": []}

    async def create_otp(self, i_account, delivery_channel):
        self.created_otps.append(i_account)
        return {"success": 1}

    def current_access_token(self):
        return None

    async def get_env_info(self):
        return {"email": "pbx@example.com"}

    async def get_version(self):
        return "MR100"


class FakeAccountAPI:
    def __init__(self):
        self.logins = []

    async def login(self, login, password=None, token=None):
        self.logins.append((login, password))
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}


class FakeOTPStorage:
    def __init__(self):
        self.stored = []

    def store(self, otp_id, i_account, user_id, bss_token=None):
        self.stored.append((i_account, user_id))


def adapter(addons=(ALLOWED_ADDON,), allowed_addons=(ALLOWED_ADDON,), account_fields=None):
    accounts = {BENEFICIARY_ID: beneficiary(addons), str(SPONSOR_I_ACCOUNT): sponsor()}
    subject = object.__new__(PortaSwitchAdapter)
    subject._init_device_limits_cache_state()
    subject._init_env_email_cache_state()
    subject._admin_api = FakeAdminAPI(accounts, account_fields)
    subject._account_api = FakeAccountAPI()
    subject._otp_storage = FakeOTPStorage()
    subject._portaswitch_settings = types.SimpleNamespace(
        SIGNIN_CREDENTIALS=PortaSwitchSettings.model_fields["SIGNIN_CREDENTIALS"].default,
        ALLOWED_ADDONS=list(allowed_addons),
        ADMIN_API_TOKEN="token",
        DEFAULT_ACCOUNT_MAX_DEVICES=None,
        DEFAULT_ACCOUNT_MAX_DEVICE_SWITCHES=None,
        DEVICE_LIMITS_CACHE_TTL=0,
    )
    return subject


async def sign_in(subject, password=PASSWORD):
    return await subject.authenticate(UserInfo(user_id="N/A", login=BENEFICIARY_ID), password)


class TestPasswordSignIn:
    @pytest.mark.asyncio
    async def test_signs_in_as_itself_with_its_own_password(self):
        # The ticket's case: the sponsor has no password, which used to raise KeyError.
        subject = adapter(allowed_addons=())

        session = await sign_in(subject)

        assert session.user_id.root == str(BENEFICIARY_I_ACCOUNT)
        assert subject._account_api.logins == [(BENEFICIARY_ID, PASSWORD)]

    @pytest.mark.asyncio
    async def test_a_wrong_password_is_refused(self):
        subject = adapter(allowed_addons=())

        with pytest.raises(WebTritErrorException) as error:
            await sign_in(subject, password="wrong")

        assert error.value.status_code == 401

    @pytest.mark.asyncio
    async def test_its_inherited_addons_let_it_in(self):
        subject = adapter()

        session = await sign_in(subject)

        # The sponsor fixture has no add-ons, so this passes only on the beneficiary's row.
        assert session.user_id.root == str(BENEFICIARY_I_ACCOUNT)

    @pytest.mark.asyncio
    async def test_without_the_addon_it_is_kept_out(self):
        subject = adapter(addons=("Other add-on",))

        with pytest.raises(WebTritErrorException) as error:
            await sign_in(subject)

        assert error.value.code == "addon_required"
        assert subject._account_api.logins == []


class TestOTPSignIn:
    @pytest.mark.asyncio
    async def test_the_otp_is_created_for_the_beneficiary(self):
        subject = adapter()

        await subject.generate_otp(UserInfo(user_id=BENEFICIARY_ID))

        assert subject._admin_api.created_otps == [BENEFICIARY_I_ACCOUNT]
        assert subject._otp_storage.stored == [(BENEFICIARY_I_ACCOUNT, BENEFICIARY_ID)]

    @pytest.mark.asyncio
    async def test_without_the_addon_it_is_kept_out(self):
        subject = adapter(addons=())

        with pytest.raises(WebTritErrorException) as error:
            await subject.generate_otp(UserInfo(user_id=BENEFICIARY_ID))

        assert error.value.code == "addon_required"
        assert subject._admin_api.created_otps == []

    @pytest.mark.asyncio
    async def test_the_sponsor_is_not_looked_up(self):
        subject = adapter()

        await subject.generate_otp(UserInfo(user_id=BENEFICIARY_ID))

        assert subject._admin_api.lookups == [{"id": BENEFICIARY_ID}]


class TestDeviceLimits:
    @pytest.mark.asyncio
    async def test_its_own_limits_apply_not_the_sponsors(self):
        # QA's case: beneficiary 1 device, sponsor 7 - the session used to get 7.
        subject = adapter(
            allowed_addons=(),
            account_fields={
                BENEFICIARY_I_ACCOUNT: {"portaphone_max_devices": "1"},
                SPONSOR_I_ACCOUNT: {"portaphone_max_devices": "7"},
            },
        )

        session = await sign_in(subject)

        assert session.account_max_devices == 1
        assert subject._admin_api.account_field_reads == [BENEFICIARY_I_ACCOUNT]
        assert {"i_account": SPONSOR_I_ACCOUNT} not in subject._admin_api.lookups

    @pytest.mark.asyncio
    async def test_a_sponsor_without_a_limit_does_not_lift_its_own(self):
        subject = adapter(
            allowed_addons=(),
            account_fields={
                BENEFICIARY_I_ACCOUNT: {"portaphone_max_devices": "1"},
                SPONSOR_I_ACCOUNT: {"portaphone_max_devices": "0"},
            },
        )

        session = await sign_in(subject)

        assert session.account_max_devices == 1
