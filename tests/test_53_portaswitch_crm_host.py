"""The CRM of the user's company in the user info (WT-2109).

The address is a customer custom field, named by PORTASWITCH_CRM_HOST_CUSTOM_FIELD. A branch
office whose field is blank takes its main office's address. The answer is reused for
CRM_HOST_CACHE_TTL; a failed read serves the last answer, or reports "cannot tell" (None)
when there is none, so Core keeps what it knew instead of dropping the CRM.
"""

import asyncio
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
from bss.models import Crm, SipServer
from bss.types import SessionInfo, UserInfo

COMPANY = 8045
BRANCH = 8046
MAIN_OFFICE = 8040
FIELD = "CRM host"


def fields(**values):
    return {"custom_fields_values": [{"name": name, "db_value": value} for name, value in values.items()]}


def host(value):
    return fields(**{FIELD: value})


class FakeAdminAPI:
    def __init__(self, customer_fields=None, customers=None, fail=None, info_fail=None):
        self._customer_fields = customer_fields or {}
        self._customers = customers or {}
        self._fail = fail
        self._info_fail = info_fail
        self.field_reads = []
        self.info_reads = []

    async def get_customer_custom_fields_values(self, i_customer):
        self.field_reads.append(i_customer)
        if self._fail:
            raise self._fail
        return self._customer_fields.get(i_customer, {"custom_fields_values": []})

    async def get_customer_info(self, i_customer):
        self.info_reads.append(i_customer)
        if self._info_fail:
            raise self._info_fail
        return {"customer_info": self._customers.get(i_customer, {"i_customer": i_customer, "i_office_type": 1})}


def adapter(field=FIELD, **admin):
    subject = object.__new__(PortaSwitchAdapter)
    subject._init_crm_host_cache_state()
    subject._admin_api = FakeAdminAPI(**admin)
    subject._portaswitch_settings = types.SimpleNamespace(CRM_HOST_CUSTOM_FIELD=field, HIDE_BALANCE_IN_USER_INFO=True)
    return subject


def account(i_customer=COMPANY):
    return {"i_account": 1, "i_customer": i_customer}


class TestWhereTheAddressComesFrom:
    @pytest.mark.asyncio
    async def test_the_companys_field_as_entered(self):
        subject = adapter(customer_fields={COMPANY: host("  acme.zendesk.com ")})

        assert await subject._read_crm(account()) == Crm(host="acme.zendesk.com")
        assert subject._admin_api.field_reads == [COMPANY]

    @pytest.mark.asyncio
    async def test_a_company_without_one_has_no_crm(self):
        for value in (None, "", "   "):
            subject = adapter(customer_fields={COMPANY: host(value)})
            assert await subject._read_crm(account()) == Crm(host=None)

    @pytest.mark.asyncio
    async def test_the_field_name_is_the_operators(self):
        subject = adapter(field="Zendesk", customer_fields={COMPANY: fields(Zendesk="acme.zendesk.com")})

        assert await subject._read_crm(account()) == Crm(host="acme.zendesk.com")

    @pytest.mark.asyncio
    async def test_a_branch_office_without_one_takes_its_main_offices(self):
        subject = adapter(
            customer_fields={MAIN_OFFICE: host("acme.zendesk.com")},
            customers={BRANCH: {"i_office_type": 2, "i_main_office": MAIN_OFFICE}},
        )

        assert await subject._read_crm(account(BRANCH)) == Crm(host="acme.zendesk.com")
        assert subject._admin_api.field_reads == [BRANCH, MAIN_OFFICE]

    @pytest.mark.asyncio
    async def test_a_branch_office_with_its_own_keeps_it(self):
        subject = adapter(
            customer_fields={BRANCH: host("lviv.zendesk.com"), MAIN_OFFICE: host("acme.zendesk.com")},
            customers={BRANCH: {"i_office_type": 2, "i_main_office": MAIN_OFFICE}},
        )

        assert await subject._read_crm(account(BRANCH)) == Crm(host="lviv.zendesk.com")
        assert subject._admin_api.field_reads == [BRANCH]

    @pytest.mark.asyncio
    async def test_a_main_office_without_one_has_no_crm(self):
        subject = adapter(customers={MAIN_OFFICE: {"i_office_type": 3}})

        assert await subject._read_crm(account(MAIN_OFFICE)) == Crm(host=None)
        assert subject._admin_api.field_reads == [MAIN_OFFICE]

    @pytest.mark.asyncio
    async def test_switched_off_it_reads_nothing(self):
        subject = adapter(field="  ", customer_fields={COMPANY: host("acme.zendesk.com")})

        assert await subject._read_crm(account()) is None
        assert subject._admin_api.field_reads == []


class TestCache:
    @pytest.mark.asyncio
    async def test_one_read_serves_the_company_until_it_expires(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(adapter_module.time, "monotonic", lambda: now[0])
        subject = adapter(customer_fields={COMPANY: host("acme.zendesk.com")})

        await subject._read_crm(account())
        now[0] += adapter_module.CRM_HOST_CACHE_TTL - 1
        await subject._read_crm({"i_account": 2, "i_customer": COMPANY})
        assert subject._admin_api.field_reads == [COMPANY]

        now[0] += 2
        await subject._read_crm(account())
        assert subject._admin_api.field_reads == [COMPANY, COMPANY]

    @pytest.mark.asyncio
    async def test_a_company_without_a_crm_is_remembered_too(self):
        subject = adapter()

        await subject._read_crm(account())
        await subject._read_crm(account())

        assert subject._admin_api.field_reads == [COMPANY]


class TestFailedRead:
    @pytest.mark.asyncio
    async def test_with_nothing_known_it_cannot_tell(self):
        subject = adapter(fail=RuntimeError("switch down"))

        assert await subject._read_crm(account()) is None

    @pytest.mark.asyncio
    async def test_a_switch_that_does_not_answer_costs_the_crm_only(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "CRM_HOST_READ_TIMEOUT", 0.05)
        subject = adapter(customer_fields={COMPANY: host("acme.zendesk.com")})

        async def hang(i_customer):
            await asyncio.sleep(10)

        subject._admin_api.get_customer_custom_fields_values = hang

        assert await subject._read_crm(account()) is None

    @pytest.mark.asyncio
    async def test_a_company_with_an_address_does_not_need_its_office_type(self):
        subject = adapter(customer_fields={COMPANY: host("acme.zendesk.com")}, info_fail=RuntimeError("no rights"))

        assert await subject._read_crm(account()) == Crm(host="acme.zendesk.com")

    @pytest.mark.asyncio
    async def test_a_company_without_one_cannot_tell_without_its_office_type(self):
        subject = adapter(info_fail=RuntimeError("no rights"))

        assert await subject._read_crm(account()) is None

    @pytest.mark.asyncio
    async def test_it_is_not_tried_again_for_a_while(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(adapter_module.time, "monotonic", lambda: now[0])
        subject = adapter(fail=RuntimeError("switch down"))

        await subject._read_crm(account())
        now[0] += adapter_module.CRM_HOST_RETRY_AFTER - 1
        await subject._read_crm(account())
        assert subject._admin_api.field_reads == [COMPANY]

        now[0] += 2
        subject._admin_api._fail = None
        subject._admin_api._customer_fields = {COMPANY: host("acme.zendesk.com")}
        assert await subject._read_crm(account()) == Crm(host="acme.zendesk.com")

    @pytest.mark.asyncio
    async def test_it_serves_the_last_answer_however_old(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(adapter_module.time, "monotonic", lambda: now[0])
        subject = adapter(customer_fields={COMPANY: host("acme.zendesk.com")})
        await subject._read_crm(account())

        now[0] += adapter_module.CRM_HOST_CACHE_TTL * 10
        subject._admin_api._fail = RuntimeError("switch down")

        assert await subject._read_crm(account()) == Crm(host="acme.zendesk.com")


class FakeAccountAPI:
    async def get_account_info(self, access_token):
        return {
            "account_info": {
                "i_account": 1,
                "i_customer": COMPANY,
                "id": "380441234567",
                "h323_password": "sip-secret",
                "is_active": 1,
                "time_zone_name": "Europe/Kyiv",
            }
        }

    async def get_alias_list(self, access_token):
        return {"alias_list": []}


@pytest.mark.asyncio
async def test_the_user_info_carries_the_crm():
    subject = adapter(customer_fields={COMPANY: host("acme.zendesk.com")})
    subject._account_api = FakeAccountAPI()
    subject._sip_server = SipServer(host="1.2.3.4", port=5060)
    subject._settings = types.SimpleNamespace(JANUS_SIP_FORCE_TCP=False)

    user = await subject.retrieve_user(
        SessionInfo(user_id="1", access_token="at"), UserInfo(user_id="1", tenant_id="")
    )

    assert user.model_dump(mode="json")["crm"] == {"host": "acme.zendesk.com"}
