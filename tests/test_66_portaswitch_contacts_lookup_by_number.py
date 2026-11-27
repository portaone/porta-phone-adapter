"""Contact lookup by number in an office hierarchy (WT-2117).

The web dialer resolves numbers to contacts with `phone_numbers`: a caller, a page of call
history, voicemail senders, SMS peers. Each number used to be asked of every office with
get_account_list, one office after another - 416 requests per number in a hierarchy of 416
offices. Now a number costs get_account_info and one get_account_list whatever the number of
offices, and with the account cache on a number of an account already cached costs nothing.
"""
import asyncio
import os
import sys
import time

import pytest
import pytest_asyncio

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

from bss.adapters.portaswitch.adapter import ACCOUNTS_CACHE_STALE_FACTOR, PortaSwitchAdapter  # noqa: E402
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
from bss.adapters.portaswitch.types import PortaSwitchContactsSelectingMode  # noqa: E402
from bss.types import SessionInfo, UserInfo  # noqa: E402

MAIN = 100
VIEWER = 1
OTHER_CUSTOMER = 900


def number_of(office):
    return f"555{office:04d}"


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


class FakeAdminAPI:
    """A hierarchy of `offices` offices with one account each, an alias of the last branch's
    account, a closed account and an account of another customer. Counts calls per method."""

    def __init__(self, offices):
        self.offices = [MAIN + n for n in range(offices)]
        self.accounts = [account(VIEWER, MAIN, "5550001")]
        self.accounts += [account(o * 10, o, number_of(o)) for o in self.offices]
        last = self.offices[-1]
        self.accounts.append(account(last * 10 + 1, last, "5559999", billing_model=2, i_master_account=last * 10))
        self.accounts.append(account(last * 10 + 2, last, "5558888", closed=True))
        self.accounts.append(account(9000, OTHER_CUSTOMER, "5557777"))
        for row in self.accounts:
            if row["billing_model"] != 2:
                row["alias_list"] = [{"id": a["id"]} for a in self.accounts
                                     if a.get("i_master_account") == row["i_account"]]
        self.calls = {}

    def _count(self, method):
        self.calls[method] = self.calls.get(method, 0) + 1

    async def get_account_list(self, i_customer, get_total=True, limit=None, offset=0, **search_params):
        self._count("get_account_list")
        rows = [a for a in self.accounts if a["i_customer"] == i_customer
                and a["billing_model"] != 2  # get_only_real_accounts
                and not a.get("closed")]  # get_not_closed_accounts
        if "id" in search_params:
            rows = [a for a in rows if a["id"] == search_params["id"]]
        window = rows[offset:offset + limit] if limit else rows[offset:]
        return {"account_list": window, **({"total": len(rows)} if get_total else {})}

    async def get_account_info(self, i_account=None, id=None, **kwargs):
        # No customer filter, closed accounts included: it searches the whole switch.
        self._count("get_account_info")
        for a in self.accounts:
            if (i_account is not None and a["i_account"] == i_account) or (id and a["id"] == id):
                return {"account_info": a}
        return {}

    async def get_customer_info(self, i_customer):
        return {"customer_info": {"i_customer": i_customer, "i_office_type": 3 if i_customer == MAIN else 2,
                                  "i_main_office": MAIN}}

    async def get_customer_list(self, i_main_customer):
        return {"customer_list": [{"i_customer": o} for o in self.offices[1:]]}

    async def get_extensions_list(self, i_customer, **kwargs):
        self._count("get_extensions_list")
        return {"extensions_list": []}


class FakeAccountAPI:
    async def get_account_info(self, access_token):
        return {"account_info": {"i_account": VIEWER, "i_customer": MAIN}}


def make_adapter(offices, ttl=0):
    adapter = object.__new__(PortaSwitchAdapter)
    adapter._portaswitch_settings = PortaSwitchSettings(
        ADMIN_API_URL="https://pbx.example.com",
        ACCOUNT_API_URL="https://pbx.example.com",
        ADMIN_API_LOGIN="admin",
        ADMIN_API_TOKEN="token",
        SIP_SERVER_HOST="1.2.3.4",
        CONTACTS_SELECTING=PortaSwitchContactsSelectingMode.ACCOUNTS,
        CONTACTS_CACHE_TTL=ttl,
    )
    adapter._init_contacts_cache_state()
    adapter._admin_api = FakeAdminAPI(offices)
    adapter._account_api = FakeAccountAPI()
    return adapter


async def lookup(adapter, *numbers):
    session = SessionInfo(user_id=str(VIEWER), access_token="tok", refresh_token="ref")
    contacts, _ = await adapter.retrieve_contacts_v2(
        session, UserInfo(user_id=str(VIEWER)), phone_numbers=list(numbers), page=1, items_per_page=100
    )
    return sorted(int(c.user_id.root if hasattr(c.user_id, "root") else c.user_id) for c in contacts)


async def read_directory(adapter):
    session = SessionInfo(user_id=str(VIEWER), access_token="tok", refresh_token="ref")
    await adapter.retrieve_contacts_v2(session, UserInfo(user_id=str(VIEWER)), page=1, items_per_page=100)


def lookup_calls(adapter):
    calls = adapter._admin_api.calls
    return {m: calls[m] for m in ("get_account_info", "get_account_list", "get_extensions_list") if m in calls}


@pytest_asyncio.fixture(autouse=True)
async def stop_warm_tasks():
    yield
    pending = [t for t in asyncio.all_tasks()
               if t is not asyncio.current_task() and "_keep_group_warm" in repr(t.get_coro())]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


# --- on the switch: the cost does not grow with the offices -----------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("offices", [3, 50])
async def test_a_number_costs_the_same_whatever_the_number_of_offices(offices):
    adapter = make_adapter(offices)
    branch = MAIN + offices - 1
    adapter._admin_api.calls.clear()

    assert await lookup(adapter, number_of(branch)) == [branch * 10]
    assert lookup_calls(adapter) == {"get_account_info": 1, "get_account_list": 1, "get_extensions_list": 1}


@pytest.mark.asyncio
async def test_an_external_number_costs_one_request():
    adapter = make_adapter(50)
    adapter._admin_api.calls.clear()

    assert await lookup(adapter, "380441234567") == []
    assert lookup_calls(adapter) == {"get_account_info": 1, "get_extensions_list": 1}


@pytest.mark.asyncio
async def test_an_alias_finds_its_master():
    adapter = make_adapter(50)
    assert await lookup(adapter, "5559999") == [(MAIN + 49) * 10]


@pytest.mark.asyncio
async def test_a_closed_account_is_not_a_contact():
    # get_account_info finds it; the list, with its filters, does not.
    adapter = make_adapter(50)
    assert await lookup(adapter, "5558888") == []


@pytest.mark.asyncio
async def test_an_account_of_another_customer_is_not_found():
    adapter = make_adapter(50)
    assert await lookup(adapter, "5557777") == []


# --- from the cache -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_number_of_a_cached_account_costs_no_request():
    adapter = make_adapter(50, ttl=60)
    await read_directory(adapter)
    adapter._admin_api.calls.clear()

    branch = MAIN + 49
    assert await lookup(adapter, number_of(branch), "5559999") == [branch * 10]
    assert lookup_calls(adapter) == {}, "the main number and the alias are both in the cache"


@pytest.mark.asyncio
async def test_a_number_the_cache_does_not_hold_is_asked_of_the_switch():
    adapter = make_adapter(50, ttl=60)
    await read_directory(adapter)
    adapter._admin_api.accounts.append(account(7777, MAIN + 7, "5550777"))  # added since the read
    adapter._admin_api.calls.clear()

    assert await lookup(adapter, number_of(MAIN + 3), "5550777", "380441234567") == [(MAIN + 3) * 10, 7777]
    # the cached number costs nothing; the new one two requests, the external one one
    assert lookup_calls(adapter) == {"get_account_info": 2, "get_account_list": 1, "get_extensions_list": 1}


@pytest.mark.asyncio
async def test_an_entry_past_the_stale_window_is_not_used():
    adapter = make_adapter(5, ttl=60)
    await read_directory(adapter)
    for office, (stored_at, rows) in list(adapter._accounts_cache.items()):
        adapter._accounts_cache[office] = (stored_at - 60 * ACCOUNTS_CACHE_STALE_FACTOR, rows)
    adapter._admin_api.calls.clear()

    assert await lookup(adapter, number_of(MAIN + 2)) == [(MAIN + 2) * 10]
    assert lookup_calls(adapter)["get_account_info"] == 1


@pytest.mark.asyncio
async def test_with_the_cache_off_nothing_is_read_from_it():
    adapter = make_adapter(5)
    await read_directory(adapter)
    assert adapter._accounts_cache == {}
    adapter._admin_api.calls.clear()

    assert await lookup(adapter, number_of(MAIN + 2)) == [(MAIN + 2) * 10]
    assert lookup_calls(adapter)["get_account_info"] == 1
