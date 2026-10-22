"""sms_capable filter of PortaSwitchAdapter.retrieve_contacts_v2 (WT-1609).

The New SMS picker asks only for contacts it can text. PortaBilling cannot filter accounts
by "has a DID", so the adapter drops the contacts without one before it counts and pages.
The fake AdminAPI below matches `%pattern%` search params the way PortaBilling does and
serves extension rows shaped like production ones: without any DID field.
"""
import os
import sys

import pytest

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

os.environ.setdefault('PORTASWITCH_ADMIN_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ACCOUNT_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ADMIN_API_LOGIN', 'admin')
os.environ.setdefault('PORTASWITCH_ADMIN_API_TOKEN', 'token')
os.environ.setdefault('PORTASWITCH_SIP_SERVER_HOST', '1.2.3.4')

from bss.adapters.portaswitch.adapter import PortaSwitchAdapter
from bss.adapters.portaswitch.config import PortaSwitchSettings
from bss.adapters.portaswitch.types import PortaSwitchContactsSelectingMode
from bss.types import SessionInfo, UserInfo

I_CUSTOMER = 100
CURRENT_I_ACCOUNT = 5001
MAX_API_LIMIT = 1000


def make_account(i_account, did=None, alias_did=None, **overrides):
    account = {
        "i_account": i_account,
        "i_customer": I_CUSTOMER,
        "id": f"ph{i_account}",
        "extension_name": f"user{i_account}",
        "extension_id": str(i_account),
        "firstname": "John",
        "lastname": f"Last{i_account}",
        "email": f"user{i_account}@example.com",
        "sip_status": 1,
        "status": "active",
        "alias_list": [],
        "alias_did_number_list": [{"did_number": alias_did}] if alias_did else [],
    }
    if did:
        account["did_number"] = did
    account.update(overrides)
    return account


class FakeAdminAPI:
    def __init__(self, accounts):
        self.accounts = accounts
        self.list_calls = []

    async def get_account_list(self, i_customer, limit=None, offset=0, **search_params):
        self.list_calls.append({"limit": limit, **search_params})
        assert limit is None or limit <= MAX_API_LIMIT, "PortaBilling rejects limit > 1000"
        rows = self.accounts
        for field, value in search_params.items():
            needle = value.strip("%").lower()
            rows = [a for a in rows if needle in str(a.get(field, "")).lower()]
        window = rows[offset:offset + limit] if limit else rows[offset:]
        return {"account_list": window, "total": len(rows)}

    async def get_account_info(self, i_account=None, id=None, **kwargs):
        for account in self.accounts:
            if (i_account is not None and account["i_account"] == i_account) or (id and account["id"] == id):
                return {"account_info": account}
        return {}

    async def get_customer_info(self, i_customer):
        return {"customer_info": {"i_office_type": 1}}

    async def get_extensions_list(self, i_customer, **kwargs):
        # Production rows: no did_number, no alias_did_number_list, no firstname/lastname
        return {"extensions_list": [
            {"i_account": a["i_account"], "id": a["extension_id"], "type": "Account", "name": a["extension_name"]}
            for a in self.accounts
        ]}


class FakeAccountAPI:
    async def get_account_info(self, access_token):
        return {"account_info": {"i_account": CURRENT_I_ACCOUNT, "i_customer": I_CUSTOMER}}


def make_adapter(accounts, mode=PortaSwitchContactsSelectingMode.ACCOUNTS, custom_contacts=(), cache_ttl=0):
    settings = PortaSwitchSettings(
        ADMIN_API_URL='https://pbx.example.com',
        ACCOUNT_API_URL='https://pbx.example.com',
        ADMIN_API_LOGIN='admin',
        ADMIN_API_TOKEN='token',
        SIP_SERVER_HOST='1.2.3.4',
        CONTACTS_SELECTING=mode,
        CONTACTS_CUSTOM=list(custom_contacts),
        CONTACTS_CACHE_TTL=cache_ttl,
    )
    adapter = object.__new__(PortaSwitchAdapter)
    adapter._portaswitch_settings = settings
    adapter._init_contacts_cache_state()
    adapter._admin_api = FakeAdminAPI(accounts)
    adapter._account_api = FakeAccountAPI()
    return adapter


async def fetch(adapter, search=None, page=1, items_per_page=40, **kwargs):
    session = SessionInfo(user_id=str(CURRENT_I_ACCOUNT), access_token='tok', refresh_token='ref')
    user = UserInfo(user_id=str(CURRENT_I_ACCOUNT))
    return await adapter.retrieve_contacts_v2(
        session, user, search=search, page=page, items_per_page=items_per_page, **kwargs
    )


def ids(items):
    return sorted(int(c.user_id.root if hasattr(c.user_id, 'root') else c.user_id) for c in items)


def directory(count, sms_every):
    """The requesting user plus `count` accounts, every `sms_every`-th one with a DID."""
    accounts = [make_account(CURRENT_I_ACCOUNT, did="27000")]
    for n in range(count):
        i_account = 6000 + n
        accounts.append(make_account(i_account, did=f"27{i_account}" if n % sms_every == 0 else None))
    return accounts


# --- ACCOUNTS -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_without_flag_nothing_is_filtered():
    adapter = make_adapter(directory(600, sms_every=20))
    items, total = await fetch(adapter, search="john")
    assert len(items) == 40
    assert total == 601  # PortaBilling's own total, as before WT-1609
    assert {c["limit"] for c in adapter._admin_api.list_calls} == {50}


@pytest.mark.asyncio
async def test_accounts_search_returns_only_sms_contacts_with_exact_total():
    adapter = make_adapter(directory(30, sms_every=3))
    items, total = await fetch(adapter, search="john", sms_capable=True)
    assert ids(items) == [6000 + n for n in range(30) if n % 3 == 0]
    assert total == 10


@pytest.mark.asyncio
async def test_accounts_alias_only_did_counts_as_sms():
    accounts = [make_account(CURRENT_I_ACCOUNT), make_account(6001, alias_did="27123"), make_account(6002)]
    items, total = await fetch(make_adapter(accounts), search="john", sms_capable=True)
    assert ids(items) == [6001]
    assert items[0].numbers.sms == ["27123"]
    assert total == 1


@pytest.mark.asyncio
async def test_accounts_search_widens_once_when_bounded_round_is_short():
    # 600 matches, one in 20 with a DID: the bounded round (40 + 10 rows per field)
    # finds ~3 SMS contacts, so the adapter re-reads up to the API maximum.
    adapter = make_adapter(directory(600, sms_every=20))
    items, total = await fetch(adapter, search="john", sms_capable=True)
    assert len(items) == 30
    assert total == 30
    limits = sorted({c["limit"] for c in adapter._admin_api.list_calls})
    assert limits == [50, MAX_API_LIMIT]


@pytest.mark.asyncio
async def test_accounts_search_does_not_widen_when_page_is_full():
    adapter = make_adapter(directory(600, sms_every=1))
    items, total = await fetch(adapter, search="john", sms_capable=True)
    assert len(items) == 40
    assert total > 40
    assert {c["limit"] for c in adapter._admin_api.list_calls} == {50}


@pytest.mark.asyncio
async def test_accounts_search_still_truncated_reports_more_than_a_page():
    # Over 1000 matches per field even after widening: the total cannot be exact,
    # but must still say there is more than the page.
    adapter = make_adapter(directory(3000, sms_every=50))
    items, total = await fetch(adapter, search="john", sms_capable=True)
    assert len(items) == 20
    assert total > 40


@pytest.mark.asyncio
async def test_accounts_without_search_filters_whole_directory():
    adapter = make_adapter(directory(30, sms_every=3))
    items, total = await fetch(adapter, sms_capable=True)
    assert len(items) == 10
    assert total == 10


@pytest.mark.asyncio
async def test_custom_contacts_without_sms_are_dropped():
    custom = [{"name": "Help Desk", "number": "911"}]
    adapter = make_adapter(directory(3, sms_every=1), custom_contacts=custom)
    items, total = await fetch(adapter)
    assert total == 4
    items, total = await fetch(adapter, sms_capable=True)
    assert total == 3
    assert all(c.numbers.sms for c in items)


# --- EXTENSIONS ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_extensions_take_sms_numbers_from_the_account():
    accounts = [make_account(CURRENT_I_ACCOUNT), make_account(6001, did="27601"), make_account(6002)]
    adapter = make_adapter(accounts, mode=PortaSwitchContactsSelectingMode.EXTENSIONS)

    items, total = await fetch(adapter)
    by_id = {int(c.user_id.root if hasattr(c.user_id, 'root') else c.user_id): c for c in items}
    assert by_id[6001].numbers.sms == ["27601"]
    assert by_id[6002].numbers.sms == []

    items, total = await fetch(adapter, sms_capable=True)
    assert ids(items) == [6001]
    assert total == 1


# --- ACCOUNTS search over the account cache ------------------------------------

def searched_calls(adapter):
    return [c for c in adapter._admin_api.list_calls if set(c) - {"limit"}]


@pytest.mark.asyncio
async def test_cached_search_is_exact_and_costs_no_calls_once_warm():
    adapter = make_adapter(directory(600, sms_every=20), cache_ttl=300)
    items, total = await fetch(adapter, search="john", sms_capable=True)
    assert len(items) == 30
    assert total == 30
    assert searched_calls(adapter) == []

    adapter._admin_api.list_calls.clear()
    items, total = await fetch(adapter, search="last60", sms_capable=True)
    assert ids(items) == [6000, 6020, 6040, 6060, 6080]
    assert total == 5
    assert adapter._admin_api.list_calls == []


@pytest.mark.asyncio
async def test_cached_search_matches_extension_and_alias_did():
    accounts = [
        make_account(CURRENT_I_ACCOUNT),
        make_account(6001, did="27601", firstname="Ann", extension_id="777"),
        make_account(6002, did="27602", firstname="Bob", alias_list=[{"id": "27899"}]),
        make_account(6003, did="27603", firstname="Cid"),
    ]
    adapter = make_adapter(accounts, cache_ttl=300)
    items, _ = await fetch(adapter, search="777", sms_capable=True)
    assert ids(items) == [6001]
    items, _ = await fetch(adapter, search="+27899", sms_capable=True)
    assert ids(items) == [6002]


@pytest.mark.asyncio
async def test_search_without_flag_keeps_field_queries_even_with_cache():
    adapter = make_adapter(directory(10, sms_every=3), cache_ttl=300)
    await fetch(adapter, search="john")
    assert searched_calls(adapter)
