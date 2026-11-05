"""Contact lookup of a beneficiary account by its number (WT-2209).

`retrieve_contacts_v2` resolves a number's account to its master through
`i_master_account`, so that an alias number shows its owner. A beneficiary
(`billing_model` 4) carries `i_master_account` too, pointing at its sponsor, and used to be
replaced by it: a call from a beneficiary was shown under the sponsor's name. A beneficiary
is a contact of its own; only an alias is resolved.
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

I_CUSTOMER = 797
VIEWER_I_ACCOUNT = 2005005
SPONSOR_I_ACCOUNT = 2005004
BENEFICIARY_I_ACCOUNT = 2005010
ALIAS_I_ACCOUNT = 2005011
BENEFICIARY_ID = "555220922"
ALIAS_ID = "555220933"
BENEFICIARY_ALIAS_I_ACCOUNT = 2005012
BENEFICIARY_ALIAS_ID = "555220944"


def account(i_account, id, **extra):
    row = {
        "i_account": i_account,
        "i_customer": I_CUSTOMER,
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
    account(VIEWER_I_ACCOUNT, "2005005"),
    account(SPONSOR_I_ACCOUNT, "2005004"),
    account(BENEFICIARY_I_ACCOUNT, BENEFICIARY_ID, billing_model=4, i_master_account=SPONSOR_I_ACCOUNT),
    account(ALIAS_I_ACCOUNT, ALIAS_ID, billing_model=2, i_master_account=SPONSOR_I_ACCOUNT),
    account(
        BENEFICIARY_ALIAS_I_ACCOUNT, BENEFICIARY_ALIAS_ID, billing_model=2, i_master_account=BENEFICIARY_I_ACCOUNT
    ),
]


class FakeAdminAPI:
    async def get_account_list(self, i_customer, limit=None, offset=0, **search_params):
        rows = ACCOUNTS
        for field, value in search_params.items():
            needle = str(value).strip("%").lower()
            rows = [a for a in rows if needle in str(a.get(field, "")).lower()]
        # get_only_real_accounts: PortaBilling leaves alias rows out of the list
        rows = [a for a in rows if a["billing_model"] != 2]
        window = rows[offset : offset + limit] if limit else rows[offset:]
        return {"account_list": window, "total": len(rows)}

    async def get_account_info(self, i_account=None, id=None, **kwargs):
        for a in ACCOUNTS:
            if (i_account is not None and a["i_account"] == i_account) or (id and a["id"] == id):
                return {"account_info": a}
        return {}

    async def get_customer_info(self, i_customer):
        return {"customer_info": {"i_office_type": 1}}

    async def get_extensions_list(self, i_customer, **kwargs):
        return {"extensions_list": []}


class FakeAccountAPI:
    async def get_account_info(self, access_token):
        return {"account_info": {"i_account": VIEWER_I_ACCOUNT, "i_customer": I_CUSTOMER}}


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
    session = SessionInfo(user_id=str(VIEWER_I_ACCOUNT), access_token="tok", refresh_token="ref")
    contacts, _ = await make_adapter().retrieve_contacts_v2(
        session, UserInfo(user_id=str(VIEWER_I_ACCOUNT)), page=1, items_per_page=40, **kwargs
    )
    return sorted(int(c.user_id.root if hasattr(c.user_id, "root") else c.user_id) for c in contacts)


@pytest.mark.asyncio
async def test_a_beneficiary_number_finds_the_beneficiary():
    # QA's case: phone_numbers=<beneficiary> used to return only the sponsor.
    assert await fetch(phone_numbers=[BENEFICIARY_ID]) == [BENEFICIARY_I_ACCOUNT]


@pytest.mark.asyncio
async def test_a_search_by_beneficiary_number_does_not_add_the_sponsor():
    assert SPONSOR_I_ACCOUNT not in await fetch(search=BENEFICIARY_ID)


@pytest.mark.asyncio
async def test_an_alias_number_still_finds_its_master():
    assert await fetch(phone_numbers=[ALIAS_ID]) == [SPONSOR_I_ACCOUNT]


@pytest.mark.asyncio
async def test_an_alias_of_a_beneficiary_finds_the_beneficiary():
    assert await fetch(phone_numbers=[BENEFICIARY_ALIAS_ID]) == [BENEFICIARY_I_ACCOUNT]
