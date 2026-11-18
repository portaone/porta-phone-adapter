"""Contact lookup by number stays within the user's customer (WT-2333).

An alias row is not in `get_account_list` (`get_only_real_accounts`), so `retrieve_contacts_v2`
finds an alias number with `get_account_info(id=...)`, which searches the whole switch. Its
result used to be returned as is: a number of another customer showed that customer's account,
extension and status, and undid the caller's Display number. It is now kept only when its
customer is the user's own or an office of its hierarchy.
"""

import os
import sys

import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

from bss.adapters.portaswitch.adapter import PortaSwitchAdapter
from bss.adapters.portaswitch.config import PortaSwitchSettings
from bss.adapters.portaswitch.types import PortaSwitchContactsSelectingMode
from bss.types import SessionInfo, UserInfo

OWN_CUSTOMER = 797
BRANCH_CUSTOMER = 798
OTHER_CUSTOMER = 900

VIEWER = 1001
OWN_MASTER = 1002
OWN_ALIAS = 1003
BRANCH_MASTER = 1004
BRANCH_ALIAS = 1005
OTHER_MASTER = 2001
OTHER_ALIAS = 2002
OTHER_BENEFICIARY = 2003


def account(i_account, i_customer, id, **extra):
    row = {
        "i_account": i_account,
        "i_customer": i_customer,
        "id": id,
        "firstname": "User",
        "lastname": str(i_account),
        "sip_status": 1,
        "status": "active",
        "billing_model": 1,
        "alias_list": [],
        "alias_did_number_list": [],
    }
    row.update(extra)
    return row


ACCOUNTS = [
    account(VIEWER, OWN_CUSTOMER, "380931015698"),
    account(OWN_MASTER, OWN_CUSTOMER, "380931015500"),
    account(OWN_ALIAS, OWN_CUSTOMER, "380931015501", billing_model=2, i_master_account=OWN_MASTER),
    account(BRANCH_MASTER, BRANCH_CUSTOMER, "380931015700"),
    account(BRANCH_ALIAS, BRANCH_CUSTOMER, "380931015701", billing_model=2, i_master_account=BRANCH_MASTER),
    account(OTHER_MASTER, OTHER_CUSTOMER, "380931015601", extension_id="601"),
    account(OTHER_ALIAS, OTHER_CUSTOMER, "380931015602", billing_model=2, i_master_account=OTHER_MASTER),
    account(OTHER_BENEFICIARY, OTHER_CUSTOMER, "380931015603", billing_model=4, i_master_account=OTHER_MASTER),
]


class FakeAdminAPI:
    async def get_account_list(self, i_customer, limit=None, offset=0, **search_params):
        rows = [a for a in ACCOUNTS if a["i_customer"] == i_customer]
        for field, value in search_params.items():
            needle = str(value).strip("%").lower()
            rows = [a for a in rows if needle in str(a.get(field, "")).lower()]
        # get_only_real_accounts: PortaBilling leaves alias rows out of the list
        rows = [a for a in rows if a["billing_model"] != 2]
        window = rows[offset : offset + limit] if limit else rows[offset:]
        return {"account_list": window, "total": len(rows)}

    async def get_account_info(self, i_account=None, id=None, **kwargs):
        # No customer filter: get_account_info searches the whole switch.
        for a in ACCOUNTS:
            if (i_account is not None and a["i_account"] == i_account) or (id and a["id"] == id):
                return {"account_info": a}
        return {}

    async def get_customer_info(self, i_customer):
        return {"customer_info": {"i_office_type": 3}}

    async def get_customer_list(self, i_main_customer):
        return {"customer_list": [{"i_customer": BRANCH_CUSTOMER}]}

    async def get_extensions_list(self, i_customer, **kwargs):
        return {"extensions_list": []}


class FakeAccountAPI:
    async def get_account_info(self, access_token):
        return {"account_info": {"i_account": VIEWER, "i_customer": OWN_CUSTOMER}}


def make_adapter():
    adapter = object.__new__(PortaSwitchAdapter)
    adapter._portaswitch_settings = PortaSwitchSettings(
        ADMIN_API_URL="https://pbx.example.com",
        ACCOUNT_API_URL="https://pbx.example.com",
        ADMIN_API_LOGIN="admin",
        ADMIN_API_TOKEN="token",
        SIP_SERVER_HOST="1.2.3.4",
        CONTACTS_SELECTING=PortaSwitchContactsSelectingMode.ACCOUNTS,
        CONTACTS_CACHE_TTL=0,
    )
    adapter._init_contacts_cache_state()
    adapter._admin_api = FakeAdminAPI()
    adapter._account_api = FakeAccountAPI()
    return adapter


async def fetch(**kwargs):
    session = SessionInfo(user_id=str(VIEWER), access_token="tok", refresh_token="ref")
    contacts, _ = await make_adapter().retrieve_contacts_v2(
        session, UserInfo(user_id=str(VIEWER)), page=1, items_per_page=40, **kwargs
    )
    return sorted(int(c.user_id.root if hasattr(c.user_id, "root") else c.user_id) for c in contacts)


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["380931015601", "380931015602", "380931015603"])
async def test_a_number_of_another_customer_is_not_found(number):
    # QA's case: the main number, its alias (the Display number) and a beneficiary.
    assert await fetch(phone_numbers=[number]) == []


@pytest.mark.asyncio
async def test_an_alias_of_the_own_customer_finds_its_master():
    assert await fetch(phone_numbers=["380931015501"]) == [OWN_MASTER]


@pytest.mark.asyncio
async def test_an_alias_of_a_branch_office_finds_its_master():
    assert await fetch(phone_numbers=["380931015701"]) == [BRANCH_MASTER]


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["380931015602", "380931015603"])
async def test_a_search_does_not_list_an_account_of_another_customer(number):
    assert await fetch(search=number) == []


@pytest.mark.asyncio
async def test_a_search_by_own_alias_still_lists_its_master():
    assert OWN_MASTER in await fetch(search="380931015501")
