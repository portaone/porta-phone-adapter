"""Device and session limits on sign-in (WT-2005).

Every sign-in that hands Core a session carries the PortaBilling i_customer and four
limits read from custom fields of the master account and its customer. An account field
missing falls back to the configured default, a customer field to None ("no limit"), and
0 means "no limit" too, an account's own 0 still overriding the default. A read that fails never blocks the sign-in: its fields count as unset,
while whatever the other read returned is still used.
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
from bss.adapters.portaswitch.config import PortaSwitchSettings
from bss.models import OtpId, SessionResponse
from bss.types import OTPVerifyRequest, UserInfo
from report_error import WebTritErrorException

MASTER_I_ACCOUNT = 102398
MASTER_ID = "555020"
ALIAS_ID = "555021"
I_CUSTOMER = 7001
PASSWORD = "secret"
OTP_ID = "otp-1"


def master():
    return {
        "i_account": MASTER_I_ACCOUNT,
        "i_customer": I_CUSTOMER,
        "id": MASTER_ID,
        "login": MASTER_ID,
        "password": PASSWORD,
    }


def alias():
    """An alias row: no custom fields of its own, pointing at its master."""
    return {
        "i_account": 102399,
        "i_customer": I_CUSTOMER,
        "id": ALIAS_ID,
        "login": ALIAS_ID,
        "password": PASSWORD,
        "i_master_account": MASTER_I_ACCOUNT,
    }


def fields(**values):
    return {"custom_fields_values": [{"name": name, "db_value": value} for name, value in values.items()]}


class FakeAdminAPI:
    def __init__(self, accounts, account_fields=None, customer_fields=None,
                 fail_account=None, fail_customer=None, fail_lookup=None):
        self._accounts = accounts
        self._account_fields = account_fields or {}
        self._customer_fields = customer_fields or {}
        self._fail_account = fail_account
        self._fail_customer = fail_customer
        self._fail_lookup = fail_lookup
        self.lookups = []
        self.account_field_reads = []
        self.customer_field_reads = []

    async def get_account_info(self, **params):
        self.lookups.append(params)
        if self._fail_lookup and "i_account" in params:
            raise self._fail_lookup
        key = params.get("id", params.get("login", params.get("i_account")))
        return {"account_info": self._accounts.get(str(key))}

    async def get_account_custom_fields_values(self, i_account):
        self.account_field_reads.append(i_account)
        if self._fail_account:
            raise self._fail_account
        return self._account_fields.get(i_account, {"custom_fields_values": []})

    async def get_customer_custom_fields_values(self, i_customer):
        self.customer_field_reads.append(i_customer)
        if self._fail_customer:
            raise self._fail_customer
        return self._customer_fields.get(i_customer, {"custom_fields_values": []})

    async def verify_otp(self, otp_token, bss_token=None):
        return {"success": 1}

    async def get_version(self):
        return "MR100"


class FakeAccountAPI:
    def __init__(self, account_info=None):
        self._account_info = account_info

    async def login(self, login, password=None, token=None):
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    async def get_account_info(self, access_token):
        return {"account_info": self._account_info}


class FakeOTPStorage:
    def __init__(self, entries):
        self._entries = entries

    def retrieve(self, otp_id):
        return self._entries.get(otp_id, (None, None, None))

    def delete(self, otp_id):
        self._entries.pop(otp_id, None)


def adapter(accounts=None, default_devices=None, default_switches=None, cache_ttl=0, **admin):
    subject = object.__new__(PortaSwitchAdapter)
    subject._init_device_limits_cache_state()
    subject._admin_api = FakeAdminAPI(accounts if accounts is not None else {MASTER_ID: master()}, **admin)
    subject._account_api = FakeAccountAPI(master())
    subject._otp_storage = FakeOTPStorage({OTP_ID: (MASTER_I_ACCOUNT, MASTER_ID, None)})
    subject._otp_settings = types.SimpleNamespace(IGNORE_ACCOUNTS=[])
    subject._portaswitch_settings = types.SimpleNamespace(
        SIGNIN_CREDENTIALS=PortaSwitchSettings.model_fields["SIGNIN_CREDENTIALS"].default,
        ALLOWED_ADDONS=[],
        ADMIN_API_TOKEN="token",
        DEFAULT_ACCOUNT_MAX_DEVICES=default_devices,
        DEFAULT_ACCOUNT_MAX_DEVICE_SWITCHES=default_switches,
        DEVICE_LIMITS_CACHE_TTL=cache_ttl,
    )
    return subject


def limits(session):
    return {
        name: getattr(session, name)
        for name in (
            "customer_id",
            "account_max_devices",
            "customer_max_devices",
            "account_max_device_switches",
            "customer_max_device_switches",
        )
    }


async def sign_in(subject, login=MASTER_ID):
    return await subject.authenticate(UserInfo(user_id="N/A", login=login), PASSWORD)


# --------------------------------------------------------------------------- #
# Where the values come from
# --------------------------------------------------------------------------- #


class TestLimitValues:
    @pytest.mark.asyncio
    async def test_values_come_from_the_custom_fields(self):
        subject = adapter(
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="3", portaphone_max_device_switches="5")},
            customer_fields={I_CUSTOMER: fields(portaphone_max_devices="50", portaphone_max_device_switches=" 10 ")},
            default_devices=1,
            default_switches=1,
        )

        session = await sign_in(subject)

        assert limits(session) == {
            "customer_id": str(I_CUSTOMER),
            "account_max_devices": 3,
            "customer_max_devices": 50,
            "account_max_device_switches": 5,
            "customer_max_device_switches": 10,
        }

    @pytest.mark.asyncio
    async def test_missing_account_fields_fall_back_to_the_defaults(self):
        subject = adapter(
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="", other_field="9")},
            default_devices=2,
            default_switches=4,
        )

        session = await sign_in(subject)

        assert session.account_max_devices == 2
        assert session.account_max_device_switches == 4

    @pytest.mark.asyncio
    async def test_missing_fields_without_defaults_mean_no_limit(self):
        subject = adapter()

        session = await sign_in(subject)

        assert limits(session) == {
            "customer_id": str(I_CUSTOMER),
            "account_max_devices": None,
            "customer_max_devices": None,
            "account_max_device_switches": None,
            "customer_max_device_switches": None,
        }

    @pytest.mark.asyncio
    async def test_customer_fields_have_no_default(self):
        # The configured defaults are for the account level only.
        subject = adapter(default_devices=2, default_switches=4)

        session = await sign_in(subject)

        assert session.customer_max_devices is None
        assert session.customer_max_device_switches is None

    @pytest.mark.asyncio
    async def test_zero_means_no_limit_and_overrides_the_default(self):
        subject = adapter(
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="0", portaphone_max_device_switches="0")},
            customer_fields={I_CUSTOMER: fields(portaphone_max_devices="0", portaphone_max_device_switches="0")},
            default_devices=5,
            default_switches=5,
        )

        session = await sign_in(subject)

        assert limits(session) == {
            "customer_id": str(I_CUSTOMER),
            "account_max_devices": None,
            "customer_max_devices": None,
            "account_max_device_switches": None,
            "customer_max_device_switches": None,
        }

    @pytest.mark.asyncio
    async def test_a_default_of_zero_means_no_limit(self):
        subject = adapter(default_devices=0, default_switches=0)

        session = await sign_in(subject)

        assert session.account_max_devices is None
        assert session.account_max_device_switches is None

    @pytest.mark.asyncio
    async def test_a_non_integer_value_is_ignored(self, caplog):
        subject = adapter(
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="three", portaphone_max_device_switches="-1")},
            customer_fields={I_CUSTOMER: fields(portaphone_max_devices="2.5")},
            default_devices=7,
        )

        session = await sign_in(subject)

        assert session.account_max_devices == 7
        assert session.account_max_device_switches is None
        assert session.customer_max_devices is None
        assert "portaphone_max_devices='three'" in caplog.text

    @pytest.mark.asyncio
    async def test_limits_reach_the_json(self):
        # /session answers through SessionResponse; the fields must survive that model.
        subject = adapter(account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="2")})

        session = await sign_in(subject)
        body = SessionResponse.model_validate(session.model_dump()).model_dump(mode="json")

        assert body["customer_id"] == str(I_CUSTOMER)
        assert body["account_max_devices"] == 2
        assert body["customer_max_devices"] is None


# --------------------------------------------------------------------------- #
# An alias reads the master's fields
# --------------------------------------------------------------------------- #


class TestAlias:
    @pytest.mark.asyncio
    async def test_alias_sign_in_reads_the_master_fields(self):
        subject = adapter(
            accounts={ALIAS_ID: alias(), str(MASTER_I_ACCOUNT): master()},
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="3")},
        )

        session = await sign_in(subject, login=ALIAS_ID)

        assert session.account_max_devices == 3
        assert subject._admin_api.account_field_reads == [MASTER_I_ACCOUNT]
        assert subject._admin_api.customer_field_reads == [I_CUSTOMER]

    @pytest.mark.asyncio
    async def test_master_already_resolved_is_not_looked_up_again(self):
        subject = adapter(accounts={ALIAS_ID: alias(), str(MASTER_I_ACCOUNT): master()})

        await sign_in(subject, login=ALIAS_ID)

        # One lookup by login, one for the master - none added by the device limits.
        assert subject._admin_api.lookups == [{"login": ALIAS_ID}, {"i_account": MASTER_I_ACCOUNT}]

    @pytest.mark.asyncio
    async def test_an_alias_record_is_resolved_to_its_master(self):
        subject = adapter(
            accounts={str(MASTER_I_ACCOUNT): master()},
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="3")},
        )

        result = await subject._read_device_limits(alias())

        assert result["account_max_devices"] == 3
        assert subject._admin_api.account_field_reads == [MASTER_I_ACCOUNT]


# --------------------------------------------------------------------------- #
# The custom fields cannot be read
# --------------------------------------------------------------------------- #


class TestFailure:
    BOTH = dict(
        account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="3", portaphone_max_device_switches="5")},
        customer_fields={I_CUSTOMER: fields(portaphone_max_devices="50", portaphone_max_device_switches="10")},
        default_devices=2,
        default_switches=4,
    )

    @pytest.mark.asyncio
    async def test_both_reads_failing_fall_back_to_the_defaults(self, caplog):
        subject = adapter(
            fail_account=WebTritErrorException(500, "boom"), fail_customer=TimeoutError(), **self.BOTH
        )

        session = await sign_in(subject)

        assert session.access_token.root == "at"
        assert limits(session) == {
            "customer_id": str(I_CUSTOMER),
            "account_max_devices": 2,
            "customer_max_devices": None,
            "account_max_device_switches": 4,
            "customer_max_device_switches": None,
        }
        assert "Cannot read the custom fields" in caplog.text

    @pytest.mark.asyncio
    async def test_a_failed_customer_read_keeps_the_account_values(self):
        subject = adapter(fail_customer=WebTritErrorException(500, "boom"), **self.BOTH)

        session = await sign_in(subject)

        assert limits(session) == {
            "customer_id": str(I_CUSTOMER),
            "account_max_devices": 3,
            "customer_max_devices": None,
            "account_max_device_switches": 5,
            "customer_max_device_switches": None,
        }

    @pytest.mark.asyncio
    async def test_a_failed_account_read_keeps_the_customer_values(self):
        subject = adapter(fail_account=TimeoutError(), **self.BOTH)

        session = await sign_in(subject)

        assert limits(session) == {
            "customer_id": str(I_CUSTOMER),
            "account_max_devices": 2,
            "customer_max_devices": 50,
            "account_max_device_switches": 4,
            "customer_max_device_switches": 10,
        }

    @pytest.mark.asyncio
    async def test_a_failed_master_lookup_falls_back_to_the_defaults(self):
        subject = adapter(fail_lookup=WebTritErrorException(500, "boom"), **self.BOTH)

        result = await subject._read_device_limits(alias())

        assert result == {
            "customer_id": str(I_CUSTOMER),
            "account_max_devices": 2,
            "customer_max_devices": None,
            "account_max_device_switches": 4,
            "customer_max_device_switches": None,
        }
        # The alias row has no fields of its own, so nothing is read for it.
        assert subject._admin_api.account_field_reads == []

    @pytest.mark.asyncio
    async def test_a_vanished_master_falls_back_to_the_defaults(self):
        subject = adapter(accounts={}, **self.BOTH)

        result = await subject._read_device_limits(alias())

        assert result["account_max_devices"] == 2
        assert result["customer_id"] == str(I_CUSTOMER)

    @pytest.mark.asyncio
    async def test_otp_sign_in_proceeds_when_the_reads_fail(self):
        subject = adapter(
            accounts={str(MASTER_I_ACCOUNT): master()},
            fail_account=WebTritErrorException(500, "boom"),
            fail_customer=WebTritErrorException(500, "boom"),
            default_devices=2,
        )

        session = await subject.validate_otp(OTPVerifyRequest(otp_id=OtpId(OTP_ID), code="1234"))

        assert session.access_token.root == "at"
        assert session.account_max_devices == 2


# --------------------------------------------------------------------------- #
# Cache of the custom-field reads
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(adapter_module, "time", fake)
    return fake


class TestCache:
    @pytest.mark.asyncio
    async def test_a_hit_within_the_ttl_reads_nothing(self, clock):
        subject = adapter(
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="3")},
            customer_fields={I_CUSTOMER: fields(portaphone_max_devices="50")},
            cache_ttl=60,
        )

        await sign_in(subject)
        clock.now += 59
        session = await sign_in(subject)

        assert session.account_max_devices == 3
        assert session.customer_max_devices == 50
        assert subject._admin_api.account_field_reads == [MASTER_I_ACCOUNT]
        assert subject._admin_api.customer_field_reads == [I_CUSTOMER]

    @pytest.mark.asyncio
    async def test_an_expired_entry_is_read_again(self, clock):
        account_fields = {MASTER_I_ACCOUNT: fields(portaphone_max_devices="3")}
        subject = adapter(account_fields=account_fields, cache_ttl=60)

        await sign_in(subject)
        account_fields[MASTER_I_ACCOUNT] = fields(portaphone_max_devices="1")
        clock.now += 60
        session = await sign_in(subject)

        assert session.account_max_devices == 1
        assert subject._admin_api.account_field_reads == [MASTER_I_ACCOUNT, MASTER_I_ACCOUNT]
        assert subject._admin_api.customer_field_reads == [I_CUSTOMER, I_CUSTOMER]

    @pytest.mark.asyncio
    async def test_ttl_zero_always_reads(self, clock):
        subject = adapter(cache_ttl=0)

        await sign_in(subject)
        await sign_in(subject)

        assert subject._admin_api.account_field_reads == [MASTER_I_ACCOUNT, MASTER_I_ACCOUNT]
        assert subject._admin_api.customer_field_reads == [I_CUSTOMER, I_CUSTOMER]
        assert subject._account_limits_cache == {}
        assert subject._customer_limits_cache == {}

    @pytest.mark.asyncio
    async def test_a_failure_is_not_cached(self, clock):
        subject = adapter(
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="3")},
            fail_account=TimeoutError(),
            default_devices=2,
            cache_ttl=60,
        )

        first = await sign_in(subject)
        subject._admin_api._fail_account = None
        second = await sign_in(subject)

        assert first.account_max_devices == 2
        assert second.account_max_devices == 3
        assert subject._admin_api.account_field_reads == [MASTER_I_ACCOUNT, MASTER_I_ACCOUNT]
        # The customer read succeeded the first time and is served from the cache.
        assert subject._admin_api.customer_field_reads == [I_CUSTOMER]

    @pytest.mark.asyncio
    async def test_the_customer_entry_serves_every_account_of_the_customer(self, clock):
        other = dict(master(), i_account=MASTER_I_ACCOUNT + 10, id="555030", login="555030")
        subject = adapter(
            accounts={MASTER_ID: master(), "555030": other},
            customer_fields={I_CUSTOMER: fields(portaphone_max_devices="50")},
            cache_ttl=60,
        )

        first = await sign_in(subject, login=MASTER_ID)
        second = await sign_in(subject, login="555030")

        assert first.customer_max_devices == second.customer_max_devices == 50
        assert subject._admin_api.account_field_reads == [MASTER_I_ACCOUNT, MASTER_I_ACCOUNT + 10]
        assert subject._admin_api.customer_field_reads == [I_CUSTOMER]

    @pytest.mark.asyncio
    async def test_the_cache_is_bounded(self, clock, monkeypatch):
        monkeypatch.setattr(adapter_module, "DEVICE_LIMITS_CACHE_MAX", 2)
        subject = adapter(cache_ttl=60)
        read = subject._admin_api.get_account_custom_fields_values

        for i_account in (1, 2, 3):
            clock.now += 1
            await subject._cached_field_limits(subject._account_limits_cache, i_account, read)

        # The stalest entry made room for the newest.
        assert list(subject._account_limits_cache) == [2, 3]


# --------------------------------------------------------------------------- #
# The other sign-in paths
# --------------------------------------------------------------------------- #


class TestOtherPaths:
    @pytest.mark.asyncio
    async def test_otp_sign_in_carries_the_limits(self):
        subject = adapter(
            accounts={str(MASTER_I_ACCOUNT): master()},
            account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="3")},
        )

        session = await subject.validate_otp(OTPVerifyRequest(otp_id=OtpId(OTP_ID), code="1234"))

        assert session.customer_id == str(I_CUSTOMER)
        assert session.account_max_devices == 3
        # The record read for the login is the one the limits use.
        assert subject._admin_api.lookups == [{"i_account": str(MASTER_I_ACCOUNT)}]

    @pytest.mark.asyncio
    async def test_signup_carries_the_limits(self):
        subject = adapter(customer_fields={I_CUSTOMER: fields(portaphone_max_device_switches="6")})
        user_data = types.SimpleNamespace(model_dump=lambda: {"access_token": "at", "refresh_token": "rt"})

        session = await subject.signup(user_data)

        assert session.customer_id == str(I_CUSTOMER)
        assert session.customer_max_device_switches == 6

    @pytest.mark.asyncio
    async def test_refresh_carries_no_limits(self):
        subject = adapter(account_fields={MASTER_I_ACCOUNT: fields(portaphone_max_devices="3")})
        subject._settings = types.SimpleNamespace(ENABLE_ON_DEMAND_SESSION_MIGRATION=False)

        async def refresh(refresh_token):
            return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

        subject._account_api.refresh = refresh

        session = await subject.refresh_session("rt")

        assert session.account_max_devices is None
        assert subject._admin_api.account_field_reads == []


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_blank_defaults_mean_no_limit(self, monkeypatch):
        monkeypatch.setenv("PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICES", "")
        monkeypatch.setenv("PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICE_SWITCHES", " ")
        monkeypatch.setenv("PORTASWITCH_DEVICE_LIMITS_CACHE_TTL", "")

        settings = PortaSwitchSettings()

        assert settings.DEFAULT_ACCOUNT_MAX_DEVICES is None
        assert settings.DEFAULT_ACCOUNT_MAX_DEVICE_SWITCHES is None
        assert settings.DEVICE_LIMITS_CACHE_TTL == 60

    def test_values_are_read(self, monkeypatch):
        monkeypatch.setenv("PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICES", "0")
        monkeypatch.setenv("PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICE_SWITCHES", "3")
        monkeypatch.setenv("PORTASWITCH_DEVICE_LIMITS_CACHE_TTL", "0")

        settings = PortaSwitchSettings()

        assert settings.DEFAULT_ACCOUNT_MAX_DEVICES == 0
        assert settings.DEFAULT_ACCOUNT_MAX_DEVICE_SWITCHES == 3
        assert settings.DEVICE_LIMITS_CACHE_TTL == 0

    @pytest.mark.parametrize("value", ["many", "-1"])
    def test_an_invalid_default_fails_start_up(self, monkeypatch, value):
        monkeypatch.setenv("PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICES", value)

        with pytest.raises(ValueError):
            PortaSwitchSettings()

    @pytest.mark.parametrize("value", ["soon", "-1"])
    def test_an_invalid_cache_ttl_fails_start_up(self, monkeypatch, value):
        monkeypatch.setenv("PORTASWITCH_DEVICE_LIMITS_CACHE_TTL", value)

        with pytest.raises(ValueError):
            PortaSwitchSettings()
