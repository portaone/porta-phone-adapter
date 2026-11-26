"""Reading a customer's accounts without `get_total` (WT-2117).

On MySQL `get_total=1` makes PortaBilling count the accounts of the whole installation on
every call, and a hierarchy of hundreds of one-account offices paid that once per office.
These tests pin that the accounts are still read whole at every size, while the count is
asked for only after a full page.
"""
import asyncio
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

from bss.adapters.portaswitch.adapter import (ACCOUNT_LIST_PAGE,  # noqa: E402
                                              PortaSwitchAdapter)
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402

MAIN = 100
BRANCHES = [101, 102, 103]


def account(i_account, i_customer=MAIN):
    return {"i_account": i_account, "i_customer": i_customer, "id": f"1000{i_account}"}


class FakeAdminAPI:
    """Account/get_account_list with PortaBilling's paging: `total` only when asked for,
    a limit above 1000 refused. Counts every call and the most in flight at once."""

    def __init__(self, accounts_by_customer, total_ever=True, ignore_offset=False,
                 branches=(), office_type=None, delay=0.0):
        self.accounts_by_customer = accounts_by_customer
        self.total_ever = total_ever  # False: a switch that never answers `total`
        self.ignore_offset = ignore_offset
        self.branches = list(branches)
        self.office_type = office_type or {}
        self.delay = delay
        self.list_calls = []
        self.customer_info_calls = []
        self.customer_list_calls = []
        self.fail_customer_list = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_account_list(self, i_customer, get_total=True, limit=None, offset=0, **search_params):
        assert limit is None or limit <= ACCOUNT_LIST_PAGE, "PortaBilling rejects limit > 1000"
        self.list_calls.append({"i_customer": i_customer, "get_total": get_total, "offset": offset,
                                "limit": limit, **search_params})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            rows = self.accounts_by_customer.get(i_customer, [])
            start = 0 if self.ignore_offset else offset
            window = rows[start:start + limit] if limit else rows[start:]
            answer = {"account_list": window}
            if get_total and self.total_ever:
                answer["total"] = len(rows)
            return answer
        finally:
            self.in_flight -= 1

    async def get_customer_info(self, i_customer):
        self.customer_info_calls.append(i_customer)
        office_type = self.office_type.get(i_customer, 1)
        info = {"i_customer": i_customer, "i_office_type": office_type}
        if office_type == 2:
            info["i_main_office"] = MAIN
        return {"customer_info": info}

    async def get_customer_list(self, i_main_customer):
        self.customer_list_calls.append(i_main_customer)
        if self.fail_customer_list:
            self.fail_customer_list -= 1
            raise RuntimeError("switch unavailable")
        return {"customer_list": [{"i_customer": c} for c in self.branches]}


def make_adapter(ttl, api):
    adapter = object.__new__(PortaSwitchAdapter)
    adapter._portaswitch_settings = PortaSwitchSettings(CONTACTS_CACHE_TTL=ttl)
    adapter._init_contacts_cache_state()
    adapter._admin_api = api
    return adapter


def age_entries(adapter, seconds):
    for office, (stored_at, rows) in list(adapter._accounts_cache.items()):
        adapter._accounts_cache[office] = (stored_at - seconds, rows)


# --- reading a customer without get_total ------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 1, 999, 1000, 1001, 2000, 2001, 4732])
async def test_every_account_is_read_at_every_size(size):
    rows = [account(n) for n in range(size)]
    api = FakeAdminAPI({MAIN: rows})
    adapter = make_adapter(0, api)

    got = await adapter._fetch_accounts_by_customer(MAIN)

    assert sorted(a["i_account"] for a in got) == list(range(size))
    assert api.list_calls[0]["get_total"] is False, "the first page must not ask for the count"
    assert sum(call["get_total"] for call in api.list_calls) <= 1, "the count is asked for once at most"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 1, 999])
async def test_a_short_first_page_costs_one_request_without_the_count(size):
    api = FakeAdminAPI({MAIN: [account(n) for n in range(size)]})
    adapter = make_adapter(0, api)

    await adapter._fetch_accounts_by_customer(MAIN)

    assert len(api.list_calls) == 1
    assert api.list_calls[0]["get_total"] is False


@pytest.mark.asyncio
async def test_a_full_first_page_asks_for_the_count_with_one_row():
    api = FakeAdminAPI({MAIN: [account(n) for n in range(4732)]})
    adapter = make_adapter(0, api)

    await adapter._fetch_accounts_by_customer(MAIN)

    first, count, *rest = api.list_calls
    assert (first["offset"], first["get_total"], first["limit"]) == (0, False, 1000)
    assert (count["get_total"], count["limit"]) == (True, 1), "the count costs one row, not a page"
    # the rest by the count, in parallel as before
    assert sorted(c["offset"] for c in rest) == [1000, 2000, 3000, 4000]
    assert not any(c["get_total"] for c in rest)


@pytest.mark.asyncio
async def test_a_customer_known_to_be_large_asks_for_the_count_with_its_first_page():
    api = FakeAdminAPI({MAIN: [account(n) for n in range(4732)]})
    adapter = make_adapter(0, api)

    got = await adapter._fetch_accounts_by_customer(MAIN, expect_large=True)

    assert len(got) == 4732
    first, *rest = api.list_calls
    assert (first["offset"], first["get_total"], first["limit"]) == (0, True, 1000)
    assert all(c["limit"] == 1000 for c in rest), "no separate count request"
    assert sorted(c["offset"] for c in rest) == [1000, 2000, 3000, 4000]


@pytest.mark.asyncio
async def test_a_large_customer_that_shrank_still_ends_at_a_short_page():
    api = FakeAdminAPI({MAIN: [account(n) for n in range(500)]})
    adapter = make_adapter(0, api)

    got = await adapter._fetch_accounts_by_customer(MAIN, expect_large=True)

    assert len(got) == 500
    assert len(api.list_calls) == 1


@pytest.mark.asyncio
async def test_a_re_read_uses_the_size_the_cache_remembers():
    api = FakeAdminAPI({MAIN: [account(n) for n in range(2500)]})
    adapter = make_adapter(60, api)

    await adapter._read_accounts_into_cache(MAIN)
    assert any(c["limit"] == 1 for c in api.list_calls), "the first read does not know the size"

    api.list_calls.clear()
    age_entries(adapter, 61)
    await adapter._read_accounts_into_cache(MAIN)
    assert api.list_calls[0]["get_total"] is True
    assert not any(c["limit"] == 1 for c in api.list_calls)


@pytest.mark.asyncio
async def test_without_a_count_it_reads_on_until_a_short_page():
    api = FakeAdminAPI({MAIN: [account(n) for n in range(2500)]}, total_ever=False)
    adapter = make_adapter(0, api)

    got = await adapter._fetch_accounts_by_customer(MAIN)

    assert len(got) == 2500
    # the first page, the count request that brought no count, then on until a short page
    assert [(c["offset"], c["limit"]) for c in api.list_calls] == [(0, 1000), (0, 1), (1000, 1000), (2000, 1000)]


@pytest.mark.asyncio
async def test_a_switch_ignoring_the_offset_cannot_keep_the_walk_going():
    api = FakeAdminAPI({MAIN: [account(n) for n in range(5000)]}, total_ever=False, ignore_offset=True)
    adapter = make_adapter(0, api)

    got = await adapter._fetch_accounts_by_customer(MAIN)

    assert len(got) == 1000
    assert len(api.list_calls) == 3, "a page with nothing new ends the walk"


@pytest.mark.asyncio
async def test_search_parameters_reach_every_page():
    api = FakeAdminAPI({MAIN: [account(n) for n in range(1500)]})
    adapter = make_adapter(0, api)

    await adapter._fetch_accounts_by_customer(MAIN, firstname="%ada%")

    assert all(call["firstname"] == "%ada%" for call in api.list_calls)
