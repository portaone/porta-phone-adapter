"""Contacts of an office hierarchy (WT-2117).

A customer of hundreds of one-account offices made every contacts refresh cost one
Account/get_account_list per office. These tests pin the four changes that make it
cheaper without changing what the contacts are:

- reading a customer's accounts without `get_total` unless a page comes back full;
- caching which offices make up a hierarchy;
- re-reading a group's stale offices with one task, FANOUT_LIMIT at a time;
- keeping a group in use warm, so that a request does not read every office itself.
"""
import asyncio
import os
import sys
import time

import pytest
import pytest_asyncio

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

os.environ.setdefault('PORTASWITCH_ADMIN_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ACCOUNT_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ADMIN_API_LOGIN', 'admin')
os.environ.setdefault('PORTASWITCH_ADMIN_API_TOKEN', 'token')
os.environ.setdefault('PORTASWITCH_SIP_SERVER_HOST', '1.2.3.4')

import bss.adapters.portaswitch.adapter as adapter_module  # noqa: E402
from bss.adapters.portaswitch.adapter import (ACCOUNT_LIST_PAGE,  # noqa: E402
                                              ACCOUNTS_CACHE_STALE_FACTOR,
                                              CONTACTS_GROUP_IDLE_TIMEOUT,
                                              FANOUT_LIMIT,
                                              OFFICE_HIERARCHY_CACHE_TTL,
                                              PortaSwitchAdapter)
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402

MAIN = 100
BRANCHES = [101, 102, 103]

# Taken before any test patches asyncio.sleep for the warm loop.
_real_sleep = asyncio.sleep


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


def hierarchy_api(**kwargs):
    office_type = {MAIN: 3, **{b: 2 for b in BRANCHES}}
    return FakeAdminAPI({}, branches=BRANCHES, office_type=office_type, **kwargs)


@pytest_asyncio.fixture(autouse=True)
async def stop_warm_tasks():
    """The warm task of a group outlives the request on purpose; a test must not leave
    it to be destroyed with the loop."""
    yield
    pending = [
        task for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and "_keep_group_warm" in repr(task.get_coro())
    ]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


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


# --- the hierarchy cache ------------------------------------------------------

@pytest.mark.asyncio
async def test_the_hierarchy_is_read_once_while_the_cache_is_on():
    api = hierarchy_api()
    adapter = make_adapter(60, api)

    for _ in range(3):
        assert await adapter._get_office_customer_ids(101) == (MAIN, [MAIN, *BRANCHES])

    assert api.customer_info_calls == [101]
    assert api.customer_list_calls == [MAIN]


@pytest.mark.asyncio
async def test_branches_of_one_hierarchy_share_its_office_list():
    api = hierarchy_api()
    adapter = make_adapter(60, api)

    for branch in BRANCHES:
        await adapter._get_office_customer_ids(branch)
    await adapter._get_office_customer_ids(MAIN)

    assert sorted(api.customer_info_calls) == sorted([*BRANCHES, MAIN])
    assert api.customer_list_calls == [MAIN]


@pytest.mark.asyncio
async def test_a_customer_outside_a_hierarchy_is_cached_too():
    api = FakeAdminAPI({})
    adapter = make_adapter(60, api)

    for _ in range(3):
        assert await adapter._get_office_customer_ids(7) == (7, [7])

    assert api.customer_info_calls == [7]
    assert api.customer_list_calls == []


@pytest.mark.asyncio
async def test_with_the_cache_off_the_hierarchy_is_read_every_time():
    api = hierarchy_api()
    adapter = make_adapter(0, api)

    for _ in range(3):
        await adapter._get_office_customer_ids(101)

    assert api.customer_info_calls == [101] * 3
    assert api.customer_list_calls == [MAIN] * 3


@pytest.mark.asyncio
async def test_a_failed_read_falls_back_as_before_and_is_not_cached():
    api = hierarchy_api()
    api.fail_customer_list = 1
    adapter = make_adapter(60, api)

    assert await adapter._get_office_customer_ids(101) == (MAIN, [MAIN])
    assert await adapter._get_office_customer_ids(101) == (MAIN, [MAIN, *BRANCHES])
    assert api.customer_list_calls == [MAIN, MAIN]


@pytest.mark.asyncio
async def test_the_hierarchy_is_read_again_once_it_expires():
    api = hierarchy_api()
    adapter = make_adapter(60, api)
    await adapter._get_office_customer_ids(101)

    for cache in (adapter._office_main_cache, adapter._office_list_cache):
        for key, (stored_at, value) in list(cache.items()):
            cache[key] = (stored_at - OFFICE_HIERARCHY_CACHE_TTL - 1, value)
    await adapter._get_office_customer_ids(101)

    assert api.customer_info_calls == [101, 101]
    assert api.customer_list_calls == [MAIN, MAIN]


@pytest.mark.asyncio
async def test_a_burst_reads_the_hierarchy_once():
    api = hierarchy_api()
    adapter = make_adapter(60, api)

    await asyncio.gather(*(adapter._get_office_customer_ids(101) for _ in range(20)))

    assert api.customer_info_calls == [101]
    assert api.customer_list_calls == [MAIN]


# --- refreshing a group of offices ---------------------------------------------

def many_offices(count):
    return {office: [account(office * 10, office)] for office in range(1000, 1000 + count)}


@pytest.mark.asyncio
async def test_without_the_cache_every_office_is_read_every_time():
    offices = many_offices(5)
    api = FakeAdminAPI(offices)
    adapter = make_adapter(0, api)

    for _ in range(2):
        await adapter._get_group_accounts(1000, list(offices))

    assert len(api.list_calls) == 10
    assert adapter._contacts_group_warm_tasks == {}


@pytest.mark.asyncio
async def test_stale_offices_are_served_at_once_and_refreshed_by_one_task():
    offices = many_offices(50)
    api = FakeAdminAPI(offices, delay=0.01)
    adapter = make_adapter(60, api)
    await adapter._get_group_accounts(1000, list(offices))
    assert len(api.list_calls) == 50
    age_entries(adapter, 61)

    served = await adapter._get_group_accounts(1000, list(offices))
    assert len(served) == 50
    assert len(api.list_calls) == 50, "a stale entry is served without waiting for a read"
    assert list(adapter._accounts_refresh_tasks) == [1000], "one refresh task for the whole group"

    api.max_in_flight = 0
    await adapter._get_group_accounts(1000, list(offices))  # while it runs: no second task
    assert list(adapter._accounts_refresh_tasks) == [1000]
    await asyncio.gather(*adapter._accounts_refresh_tasks.values())

    assert len(api.list_calls) == 100
    assert api.max_in_flight <= FANOUT_LIMIT, "the refresh must not start every office at once"


@pytest.mark.asyncio
async def test_a_hierarchy_read_goes_through_its_main_office_group():
    api = hierarchy_api()
    api.accounts_by_customer = {office: [account(office * 10, office)] for office in [MAIN, *BRANCHES]}
    adapter = make_adapter(60, api)

    main, offices = await adapter._get_office_customer_ids(102)
    lists = await adapter._get_group_accounts(main, offices)

    assert [rows[0]["i_customer"] for rows in lists] == [MAIN, *BRANCHES]
    assert adapter._contacts_groups == {MAIN: [MAIN, *BRANCHES]}


# --- keeping a group warm --------------------------------------------------------

@pytest.fixture
def fast_sleep(monkeypatch):
    """Make the warm loop's sleeps instant and record how long it asked for."""
    asked = []

    async def sleep(seconds, *args, **kwargs):
        asked.append(seconds)
        await _real_sleep(0)

    monkeypatch.setattr(adapter_module.asyncio, "sleep", sleep)
    return asked


async def settle(rounds=20):
    for _ in range(rounds):
        await _real_sleep(0)


@pytest.mark.asyncio
async def test_a_group_in_use_is_re_read_before_it_leaves_the_stale_window(fast_sleep):
    offices = many_offices(3)
    api = FakeAdminAPI(offices)
    adapter = make_adapter(60, api)
    await adapter._get_group_accounts(1000, list(offices))
    assert len(api.list_calls) == 3

    age_entries(adapter, 60 * (ACCOUNTS_CACHE_STALE_FACTOR - 1))
    await settle()

    assert len(api.list_calls) == 6, "the warm task re-reads the group without a request"
    ages = [time.monotonic() - stored_at for stored_at, _ in adapter._accounts_cache.values()]
    assert all(age < 60 for age in ages)


@pytest.mark.asyncio
async def test_young_entries_are_left_alone(fast_sleep):
    offices = many_offices(3)
    api = FakeAdminAPI(offices)
    adapter = make_adapter(60, api)
    await adapter._get_group_accounts(1000, list(offices))

    age_entries(adapter, 61)  # stale for a request, but far from the end of the window
    await settle()

    assert len(api.list_calls) == 3
    # it sleeps until the entries reach (factor - 1) x TTL: 540 - 61 seconds from now
    warm_age = 60 * (ACCOUNTS_CACHE_STALE_FACTOR - 1)
    assert fast_sleep and warm_age - 62 < fast_sleep[-1] <= warm_age - 61


@pytest.mark.asyncio
async def test_a_group_nobody_asks_for_stops_being_kept_warm(fast_sleep):
    offices = many_offices(3)
    api = FakeAdminAPI(offices)
    adapter = make_adapter(60, api)
    await adapter._get_group_accounts(1000, list(offices))
    task = adapter._contacts_group_warm_tasks[1000]

    adapter._contacts_group_last_used[1000] -= CONTACTS_GROUP_IDLE_TIMEOUT
    age_entries(adapter, 60 * ACCOUNTS_CACHE_STALE_FACTOR)
    await settle()

    assert task.done()
    assert len(api.list_calls) == 3, "an idle group is not read again"
    assert 1000 not in adapter._contacts_groups
    assert 1000 not in adapter._contacts_group_warm_tasks


@pytest.mark.asyncio
async def test_a_failed_warm_read_is_retried_a_ttl_later_not_at_once(fast_sleep):
    offices = many_offices(2)
    api = FakeAdminAPI(offices)
    adapter = make_adapter(60, api)
    await adapter._get_group_accounts(1000, list(offices))

    async def failing(i_customer, **kwargs):
        raise RuntimeError("switch unavailable")

    adapter._fetch_accounts_by_customer = failing
    age_entries(adapter, 60 * (ACCOUNTS_CACHE_STALE_FACTOR - 1))
    fast_sleep.clear()
    await settle()

    assert fast_sleep and fast_sleep[0] == 60, "after a failed round the next one is a TTL later"
    assert len(adapter._accounts_cache) == 2, "the old entries are still served"


@pytest.mark.asyncio
async def test_an_office_with_no_entry_is_left_to_the_next_request(fast_sleep):
    offices = many_offices(2)
    api = FakeAdminAPI(offices)
    adapter = make_adapter(60, api)
    await adapter._get_group_accounts(1000, list(offices))

    adapter._accounts_cache.clear()
    await settle()

    assert len(api.list_calls) == 2


@pytest.mark.asyncio
async def test_one_warm_task_per_group():
    offices = many_offices(2)
    api = FakeAdminAPI(offices)
    adapter = make_adapter(60, api)

    for _ in range(5):
        await adapter._get_group_accounts(1000, list(offices))
    await adapter._get_group_accounts(2000, [1000])

    assert sorted(adapter._contacts_group_warm_tasks) == [1000, 2000]
