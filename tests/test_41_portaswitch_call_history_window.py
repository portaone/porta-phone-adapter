"""Default look-back window for the PortaSwitch call history (WT-1932).

The adapter used to ask PortaBilling for `from_date => '1970-01-01'` /
`to_date => '9000-01-01'` whenever the client requested call history without a
date range, which is what every client does by default. PortaBilling partitions
CDR_ACCOUNTS by week on `bill_time` — the field those two parameters filter —
and at the reporting installation each non-empty partition held ~9-10M rows
across ~30 partitions, so returning 40 rows made the switch process the lot
twice over (once for the rows, once for `get_total`) and took its web services
down.

These tests pin the replacement: fill in only the bounds the client left out,
never narrow a range it asked for, and hand PortaBilling naive UTC.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

# PortaSwitchSettings is instantiated at import time and its URL/credential
# fields are mandatory; supply throwaway values before importing the adapter.
os.environ.setdefault('PORTASWITCH_ADMIN_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ACCOUNT_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ADMIN_API_LOGIN', 'admin')
os.environ.setdefault('PORTASWITCH_ADMIN_API_TOKEN', 'token')
os.environ.setdefault('PORTASWITCH_SIP_SERVER_HOST', '1.2.3.4')

from bss.adapters.portaswitch.adapter import (CALL_HISTORY_MAX_DATE,  # noqa: E402
                                              CALL_HISTORY_MIN_DATE,
                                              PortaSwitchAdapter)
from bss.adapters.portaswitch.config import (CALL_HISTORY_MAX_WINDOW_HOURS,  # noqa: E402
                                             PortaSwitchSettings)
from bss.adapters.portaswitch.api.account import AccountAPI  # noqa: E402
from bss.types import SessionInfo, UserInfo  # noqa: E402

WINDOW_DEFAULT = 24


def make_adapter(window_hours=WINDOW_DEFAULT):
    adapter = object.__new__(PortaSwitchAdapter)
    adapter._portaswitch_settings = PortaSwitchSettings(
        CALL_HISTORY_DEFAULT_WINDOW_HOURS=window_hours
    )
    return adapter


class RecordingAccountAPI:
    """Captures the arguments `retrieve_calls` passes down, returns nothing."""

    def __init__(self):
        self.calls = []

    async def get_xdr_list(self, **kwargs):
        self.calls.append(kwargs)
        return {"xdr_list": [], "total": 0}


# --- the window is a default -----------------------------------------------

def test_no_range_asked_for_yields_the_last_24_hours():
    adapter = make_adapter()
    before = datetime.utcnow()

    time_from, time_to = adapter._call_history_range(None, None)

    after = datetime.utcnow()
    assert before <= time_to <= after
    assert time_to - time_from == timedelta(hours=WINDOW_DEFAULT)


def test_only_time_from_asked_for_keeps_it_and_ends_at_now():
    """The delta-sync shape: "everything since X". X is honoured however old it
    is — this is the case a cap would have broken."""
    adapter = make_adapter()
    asked = datetime(2019, 3, 1, 10, 30)
    before = datetime.utcnow()

    time_from, time_to = adapter._call_history_range(asked, None)

    assert time_from == asked
    assert before <= time_to <= datetime.utcnow()


def test_only_time_to_asked_for_measures_the_window_back_from_it():
    adapter = make_adapter()
    asked = datetime(2024, 2, 1, 12, 0)

    time_from, time_to = adapter._call_history_range(None, asked)

    assert time_to == asked
    assert time_from == asked - timedelta(hours=WINDOW_DEFAULT)


def test_an_explicit_range_is_passed_through_untouched():
    """A range wider than the window is not narrowed: the window says how far
    back we look when nobody said, not how far back we are allowed to look."""
    adapter = make_adapter()
    asked_from = datetime(2020, 1, 1)
    asked_to = datetime(2026, 1, 1)

    assert adapter._call_history_range(asked_from, asked_to) == (asked_from, asked_to)


def test_window_of_zero_restores_the_previous_pair_exactly():
    """The escape hatch for an installation that would rather keep the old
    behaviour than change what its users see — so it has to be the old range on
    both ends, 9000-01-01 included, not just the old lower bound."""
    adapter = make_adapter(0)

    assert adapter._call_history_range(None, None) == (
        CALL_HISTORY_MIN_DATE, CALL_HISTORY_MAX_DATE
    )


# --- bounds that would overflow the datetime type ---------------------------

def test_a_time_to_at_the_start_of_the_epoch_does_not_overflow():
    """`time_to` comes straight off the query string, and subtracting the window
    from a datetime near the type's lower edge raises OverflowError — which
    nothing in the call chain catches, so it would reach the client as a 500."""
    adapter = make_adapter()

    time_from, time_to = adapter._call_history_range(None, datetime(1, 1, 1))

    assert time_from == CALL_HISTORY_MIN_DATE
    assert time_to == datetime(1, 1, 1)


def test_an_aware_bound_at_the_edge_of_the_type_does_not_overflow():
    """Shifting datetime.min by its own offset overflows too, in _as_naive_utc."""
    adapter = make_adapter()

    time_from, _ = adapter._call_history_range(
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=3))), None
    )

    assert time_from == datetime(1, 1, 1)


def test_an_enormous_window_is_clamped_to_the_sanity_bound():
    """A window this long can only be a typo. Left unbounded it would eventually
    overflow the timedelta the adapter builds from it; well before that it
    already reaches past the floor, so clamping costs nothing."""
    settings = PortaSwitchSettings(CALL_HISTORY_DEFAULT_WINDOW_HOURS="99999999")

    assert settings.CALL_HISTORY_DEFAULT_WINDOW_HOURS == CALL_HISTORY_MAX_WINDOW_HOURS


def test_a_window_past_the_floor_yields_the_floor():
    adapter = make_adapter(CALL_HISTORY_MAX_WINDOW_HOURS)

    time_from, _ = adapter._call_history_range(None, None)

    assert time_from == CALL_HISTORY_MIN_DATE


@pytest.mark.parametrize("value", ["", "   ", "abc", "-5", None])
def test_a_blank_or_invalid_window_falls_back_to_24_hours(value):
    """0 means "switch the window off", so it must take a deliberate 0 — a stray
    empty env var has to land on the default instead."""
    settings = PortaSwitchSettings(CALL_HISTORY_DEFAULT_WINDOW_HOURS=value)
    assert settings.CALL_HISTORY_DEFAULT_WINDOW_HOURS == WINDOW_DEFAULT


def test_an_explicit_zero_is_honoured():
    assert PortaSwitchSettings(CALL_HISTORY_DEFAULT_WINDOW_HOURS="0") \
        .CALL_HISTORY_DEFAULT_WINDOW_HOURS == 0


# --- timezone handling ------------------------------------------------------

def test_an_aware_range_is_converted_to_naive_utc():
    """Core casts the query parameters through an OpenAPI `date-time` schema, so
    what arrives is aware, while our own defaults are naive. Comparing or
    subtracting the two raises TypeError, and nothing on the way out turns that
    into anything but a 500."""
    adapter = make_adapter()
    tz = timezone(timedelta(hours=3))

    time_from, time_to = adapter._call_history_range(
        datetime(2026, 9, 5, 13, 0, tzinfo=tz), datetime(2026, 9, 6, 13, 0, tzinfo=tz)
    )

    assert time_from.tzinfo is None and time_to.tzinfo is None
    # 13:00 at UTC+3 is 10:00 UTC — the offset is applied, not discarded
    assert time_from == datetime(2026, 9, 5, 10, 0)
    assert time_to == datetime(2026, 9, 6, 10, 0)


def test_an_aware_time_from_alone_does_not_raise():
    """The regression guard: an aware lower bound against a naive default is
    exactly the mix that used to be unreachable code and is now routine."""
    adapter = make_adapter()

    time_from, time_to = adapter._call_history_range(
        datetime(2026, 9, 5, 13, 0, tzinfo=timezone.utc), None
    )

    assert time_from == datetime(2026, 9, 5, 13, 0)
    assert time_to.tzinfo is None


@pytest.mark.parametrize("aware_bound", ["from", "to"])
def test_one_aware_bound_beside_one_naive_bound_does_not_raise(aware_bound):
    """Each bound is normalised on its own, so a client that sends one of them
    with an offset and the other without cannot produce the aware/naive mix that
    raises TypeError on the first comparison."""
    adapter = make_adapter()
    tz = timezone(timedelta(hours=3))
    asked_from = datetime(2024, 1, 1, 10, 0, tzinfo=tz) if aware_bound == "from" \
        else datetime(2024, 1, 1, 10, 0)
    asked_to = datetime(2024, 2, 1, 10, 0, tzinfo=tz) if aware_bound == "to" \
        else datetime(2024, 2, 1, 10, 0)

    time_from, time_to = adapter._call_history_range(asked_from, asked_to)

    assert time_from.tzinfo is None and time_to.tzinfo is None
    assert time_from < time_to


def test_an_aware_time_to_anchors_the_window_in_utc():
    adapter = make_adapter()
    tz = timezone(timedelta(hours=-7))

    time_from, time_to = adapter._call_history_range(
        None, datetime(2026, 9, 6, 0, 0, tzinfo=tz)
    )

    assert time_to == datetime(2026, 9, 6, 7, 0)
    assert time_from == datetime(2026, 9, 5, 7, 0)


# --- what actually reaches PortaBilling -------------------------------------

@pytest.mark.asyncio
async def test_retrieve_calls_sends_the_resolved_range():
    """End to end through `retrieve_calls`: the 1970/9000 sentinels are gone from
    the wire, and the switch is handed the window instead."""
    adapter = make_adapter()
    api = RecordingAccountAPI()
    adapter._account_api = api

    items, total = await adapter.retrieve_calls(
        SessionInfo(user_id="1", access_token="token", refresh_token="refresh"),
        UserInfo(user_id="1"),
        page=1,
        items_per_page=40,
    )

    assert (items, total) == ([], 0)
    sent = api.calls[0]
    assert sent["time_to"] - sent["time_from"] == timedelta(hours=WINDOW_DEFAULT)
    assert sent["time_from"].tzinfo is None
    assert sent["time_from"] != datetime(1970, 1, 1)
    assert sent["time_to"] != datetime(9000, 1, 1)


@pytest.mark.asyncio
async def test_retrieve_calls_forwards_an_aware_range_as_naive_utc():
    adapter = make_adapter()
    api = RecordingAccountAPI()
    adapter._account_api = api

    await adapter.retrieve_calls(
        SessionInfo(user_id="1", access_token="token", refresh_token="refresh"),
        UserInfo(user_id="1"),
        time_from=datetime(2026, 9, 1, 6, 0, tzinfo=timezone(timedelta(hours=2))),
        time_to=datetime(2026, 9, 2, 6, 0, tzinfo=timezone(timedelta(hours=2))),
    )

    sent = api.calls[0]
    assert sent["time_from"] == datetime(2026, 9, 1, 4, 0)
    assert sent["time_to"] == datetime(2026, 9, 2, 4, 0)


# --- the payload PortaBilling actually receives ------------------------------

@pytest.mark.asyncio
async def test_get_xdr_list_pins_the_payload_sent_to_the_switch():
    """`retrieve_calls` is stubbed at the adapter boundary everywhere else, so
    without this nothing covers the params dict itself: the `%Y-%m-%d %H:%M:%S`
    format the dates go out in, and `get_total => 1`, which is deliberately kept
    (inside a bounded window the COUNT is cheap, and dropping it would empty
    `items_total` out of the wire contract)."""
    api = object.__new__(AccountAPI)
    sent = {}

    async def fake_send_request(**kwargs):
        sent.update(kwargs)
        return {"xdr_list": [], "total": 0}

    api._AccountAPI__send_request = fake_send_request

    await api.get_xdr_list(
        access_token="token",
        page=3,
        items_per_page=40,
        time_from=datetime(2026, 9, 5, 6, 8, 39),
        time_to=datetime(2026, 9, 6, 6, 8, 39),
    )

    assert sent["module"] == "Account"
    assert sent["method"] == "get_xdr_list"
    assert sent["params"] == {
        "i_service_type": 3,
        "get_total": 1,
        "show_unsuccessful": 1,
        "limit": 40,
        "offset": 80,
        "from_date": "2026-09-05 06:08:39",
        "to_date": "2026-09-06 06:08:39",
    }


@pytest.mark.asyncio
async def test_the_sentinels_no_longer_reach_the_switch():
    """The regression this ticket is about, checked on the wire rather than on
    the resolved range."""
    adapter = make_adapter()
    api = object.__new__(AccountAPI)
    sent = {}

    async def fake_send_request(**kwargs):
        sent.update(kwargs)
        return {"xdr_list": [], "total": 0}

    api._AccountAPI__send_request = fake_send_request
    adapter._account_api = api

    await adapter.retrieve_calls(
        SessionInfo(user_id="1", access_token="token", refresh_token="refresh"),
        UserInfo(user_id="1"),
        page=1,
        items_per_page=40,
    )

    assert sent["params"]["from_date"] != "1970-01-01 00:00:00"
    assert sent["params"]["to_date"] != "9000-01-01 00:00:00"
    assert sent["params"]["get_total"] == 1
