"""A call answered by voicemail is one call history item, not two (WT-2281).

PortaSwitch writes two xDRs on the callee's account for such a call, with one call_id:
the incoming leg that rang (missed or declined) and a forwarded leg to the callee's own
number - its voicemail - that reads as accepted. The forwarded leg is dropped when the
page or the rows around it hold its incoming leg with the same CLD; a real forward
carries the target's CLD.
The rows below are the ones the reporter took from the test switch's database.
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

from bss.adapters.portaswitch.adapter import CALL_HISTORY_PAIR_MARGIN, PortaSwitchAdapter  # noqa: E402
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
from bss.adapters.portaswitch.serializer import Serializer  # noqa: E402
from bss.types import ConnectStatus, Direction, SessionInfo, UserInfo  # noqa: E402

_i_xdr = iter(range(1000, 2000))


def _xdr(call_id, bit_flags, failed, cause, cli, cld, i_account=555046, **extra) -> dict:
    xdr = {
        "i_xdr": next(_i_xdr),
        "i_account": i_account,
        "call_id": call_id,
        "h323_conf_id": "090EE081 166311DF 44E484E1 44E484E1",
        "CLI": cli,
        "CLD": cld,
        "unix_connect_time": 1794324739,
        "unix_disconnect_time": 1794324748,
        "disconnect_reason": "Normal call clearing",
        "disconnect_cause": str(cause),
        "charged_quantity": 9,
        "failed": failed,
        "bit_flags": bit_flags,
        "bill_time": "2026-11-10 15:32:19",
    }
    xdr.update(extra)
    return xdr


def _kept(xdr_list):
    return [x["i_xdr"] for x in Serializer.drop_own_number_forwards(xdr_list)]


@pytest.mark.parametrize("cause", [19, 21])
def test_the_voicemail_leg_of_a_missed_or_declined_call_is_dropped(cause):
    rang = _xdr("dFCMKGvzDRvmDWA3GhJjGGir", 8, 1, cause, "555003", "555046")
    voicemail = _xdr("dFCMKGvzDRvmDWA3GhJjGGir", 12, 0, 16, "555003", "555046", destination="UM")

    assert _kept([voicemail, rang]) == [rang["i_xdr"]]


def test_an_extension_matches_by_number_whatever_name_follows_it():
    rang = _xdr("SDcb3f901-a848r11", 72, 1, 19, "27105348647", "200 (200 - Main)", i_account=1942564)
    voicemail = _xdr("SDcb3f901-a848r11", 12, 0, 16, "27105348647", "200 (200 - Dipolelo)", i_account=1942564)

    assert _kept([rang, voicemail]) == [rang["i_xdr"]]


def test_auto_attendant_keeps_its_redirect_and_drops_its_own_ivr_leg():
    """555012 is an Auto Attendant that redirects the caller to 555013."""
    call = "p4Og2SBMMgogO7x8z7HO6LK0"
    aa_in = _xdr(call, 8, 1, 27, "555046", "555012", i_account=555012)
    aa_ivr = _xdr(call, 12, 0, 16, "555046", "555012", i_account=555012)
    aa_redirect = _xdr(call, 12, 0, 16, "555046", "555013", i_account=555012)

    assert _kept([aa_redirect, aa_ivr, aa_in]) == [aa_redirect["i_xdr"], aa_in["i_xdr"]]


def test_a_forward_to_another_number_is_kept():
    rang = _xdr("SDfollowme", 8, 1, 19, "0740350379", "2405 (2405 - Thabo)")
    to_mobile = _xdr("SDfollowme", 12, 0, 16, "0740350379", "27759528825")

    assert _kept([rang, to_mobile]) == [rang["i_xdr"], to_mobile["i_xdr"]]


def test_a_forwarded_leg_without_its_incoming_leg_is_kept():
    voicemail = _xdr("SDalone", 12, 0, 16, "555003", "555046")
    other_call = _xdr("SDother", 8, 1, 19, "555003", "555046")

    assert _kept([voicemail, other_call]) == [voicemail["i_xdr"], other_call["i_xdr"]]


def test_rows_without_call_id_never_pair():
    rang = _xdr(None, 8, 1, 19, "555003", "555046")
    voicemail = _xdr(None, 12, 0, 16, "555003", "555046")

    assert _kept([rang, voicemail]) == [rang["i_xdr"], voicemail["i_xdr"]]


def test_rows_without_cld_never_pair():
    rang = _xdr("SDnocld", 8, 1, 19, "555003", "")
    forward = _xdr("SDnocld", 12, 0, 16, "555003", None)

    assert _kept([rang, forward]) == [rang["i_xdr"], forward["i_xdr"]]


def test_outgoing_calls_are_never_dropped():
    to_voicemail = _xdr("SDown", 4, 0, 16, "555046", "555046")
    rang = _xdr("SDown", 8, 1, 19, "555003", "555046")

    assert _kept([to_voicemail, rang]) == [to_voicemail["i_xdr"], rang["i_xdr"]]


# --- end to end through retrieve_calls ----------------------------------------


class AdminAPI:
    async def get_version(self):
        return "MR128-1"


class AccountAPI:
    """The switch's whole call list, sliced by offset/limit as PortaBilling does."""

    def __init__(self, xdr_list, total=None):
        self.xdr_list = xdr_list
        self.total = len(xdr_list) if total is None else total
        self.calls = []

    async def get_xdr_list(self, **kwargs):
        self.calls.append(kwargs)
        offset = kwargs["offset"]
        return {"xdr_list": self.xdr_list[offset : offset + kwargs["limit"]], "total": self.total}


def make_adapter(account):
    adapter = object.__new__(PortaSwitchAdapter)
    adapter._portaswitch_settings = PortaSwitchSettings()
    adapter._admin_api = AdminAPI()
    adapter._account_api = account
    adapter._init_portaswitch_version_cache_state()
    return adapter


async def history(adapter, page=1, items_per_page=100):
    return await adapter.retrieve_calls(
        SessionInfo(user_id="1", access_token="token", refresh_token="refresh"),
        UserInfo(user_id="1"),
        page=page,
        items_per_page=items_per_page,
    )


def _other_calls(n, start=0):
    return [_xdr(f"SDother{start + i}", 4, 0, 16, "555046", "555003") for i in range(n)]


@pytest.mark.asyncio
async def test_history_shows_the_missed_call_once_and_keeps_the_switch_total():
    rang = _xdr("dFCMKGvzDRvmDWA3GhJjGGir", 8, 1, 19, "555003", "555046")
    voicemail = _xdr("dFCMKGvzDRvmDWA3GhJjGGir", 12, 0, 16, "555003", "555046")

    items, total = await history(make_adapter(AccountAPI([voicemail, rang], total=101)))

    assert [(i.call_id, i.direction, i.status) for i in items] == [
        ("dFCMKGvzDRvmDWA3GhJjGGir", Direction.incoming, ConnectStatus.missed)
    ]
    assert total == 101


@pytest.mark.asyncio
async def test_a_voicemail_leg_ending_a_page_finds_its_incoming_leg_on_the_next():
    voicemail = _xdr("SDsplit", 12, 0, 16, "555003", "555046")
    rang = _xdr("SDsplit", 8, 1, 19, "555003", "555046")
    calls = _other_calls(2) + [voicemail, rang] + _other_calls(3, start=2)

    page_1, _ = await history(make_adapter(AccountAPI(calls)), page=1, items_per_page=3)
    page_2, _ = await history(make_adapter(AccountAPI(calls)), page=2, items_per_page=3)

    assert [i.call_id for i in page_1] == ["SDother0", "SDother1"]
    assert [i.call_id for i in page_2] == ["SDsplit", "SDother2", "SDother3"]
    assert page_2[0].status == ConnectStatus.missed


@pytest.mark.asyncio
async def test_a_voicemail_leg_starting_a_page_finds_its_incoming_leg_on_the_previous():
    rang = _xdr("SDsplit", 8, 1, 19, "555003", "555046")
    voicemail = _xdr("SDsplit", 12, 0, 16, "555003", "555046")
    calls = _other_calls(2) + [rang, voicemail] + _other_calls(3, start=2)

    page_2, _ = await history(make_adapter(AccountAPI(calls)), page=2, items_per_page=3)

    assert [i.call_id for i in page_2] == ["SDother2", "SDother3"]


@pytest.mark.asyncio
async def test_the_page_is_read_with_a_margin_on_each_side():
    account = AccountAPI(_other_calls(100))

    await history(make_adapter(account), page=1, items_per_page=40)
    await history(make_adapter(account), page=3, items_per_page=40)
    await history(make_adapter(account), page=2, items_per_page=5)

    assert [(c["offset"], c["limit"]) for c in account.calls] == [
        (0, 40 + CALL_HISTORY_PAIR_MARGIN),
        (80 - CALL_HISTORY_PAIR_MARGIN, 40 + 2 * CALL_HISTORY_PAIR_MARGIN),
        (0, 5 + 5 + CALL_HISTORY_PAIR_MARGIN),
    ]


@pytest.mark.asyncio
async def test_rows_of_the_margin_are_never_returned():
    calls = _other_calls(30)

    items, _ = await history(make_adapter(AccountAPI(calls)), page=2, items_per_page=10)

    assert [i.call_id for i in items] == [f"SDother{i}" for i in range(10, 20)]
