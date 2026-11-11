"""Call history detects recordings by the PortaSwitch release (WT-2241, WT-2283).

From MR129 on, get_xdr_list no longer sets bit_flags bit 64 for a recorded call, so the
adapter asks get_xdr_list a second time with call_recording=1, narrowed to the bill_time
of the page, and marks the page rows it returns. with_cr_download_ids is never sent: it
lists the recording storage over SFTP and took up to a minute per page. Before MR129 the
adapter reads bit 64.
"""
import os
import sys
from datetime import datetime
from unittest.mock import patch

import pytest

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

os.environ.setdefault('PORTASWITCH_ADMIN_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ACCOUNT_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ADMIN_API_LOGIN', 'admin')
os.environ.setdefault('PORTASWITCH_ADMIN_API_TOKEN', 'token')
os.environ.setdefault('PORTASWITCH_SIP_SERVER_HOST', '1.2.3.4')

from bss.adapters.portaswitch import adapter as adapter_module  # noqa: E402
from bss.adapters.portaswitch.adapter import PortaSwitchAdapter  # noqa: E402
from bss.adapters.portaswitch.config import PortaSwitchSettings  # noqa: E402
from bss.adapters.portaswitch.exceptions import access_token_expired_error  # noqa: E402
from bss.types import SessionInfo, UserInfo  # noqa: E402
from report_error import WebTritErrorException  # noqa: E402


def _cdr(**overrides) -> dict:
    cdr = {
        "i_xdr": 129580143,
        "call_id": "b2YBUVAUT27eW4QmAd2yBSqG",
        "h323_conf_id": "062BDBD0 7366C3AD 6ED2EA7A 66D2F473",
        "CLI": "7774",
        "CLD": "7773",
        "unix_connect_time": 1757260800,
        "unix_disconnect_time": 1757260844,
        "disconnect_reason": "Caller hangup",
        "disconnect_cause": 16,
        "charged_quantity": 44,
        "failed": 0,
        "bit_flags": 4,
        "bill_time": "2025-09-07 16:00:44",
    }
    cdr.update(overrides)
    return cdr


class AdminAPI:
    def __init__(self, version="MR120-3", error=None):
        self.version = version
        self.error = error
        self.calls = 0

    async def get_version(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.version


class AccountAPI:
    """The page for a plain request; for call_recording=1, the recorded rows among
    `recorded` whose bill_time is inside the range, as PortaBilling filters them."""

    def __init__(self, xdr_list=(), recorded=(), recorded_error=None):
        self.xdr_list = list(xdr_list)
        self.recorded = list(recorded)
        self.recorded_error = recorded_error
        self.calls = []

    async def get_xdr_list(self, **kwargs):
        self.calls.append(kwargs)
        if not kwargs.get("call_recording"):
            return {"xdr_list": self.xdr_list, "total": len(self.xdr_list)}
        if self.recorded_error:
            raise self.recorded_error
        lo = kwargs["time_from"].strftime("%Y-%m-%d %H:%M:%S")
        hi = kwargs["time_to"].strftime("%Y-%m-%d %H:%M:%S")
        rows = [x for x in self.recorded if lo <= x["bill_time"] < hi]
        return {"xdr_list": rows[:kwargs["items_per_page"]]}


def make_adapter(admin, account=None):
    adapter = object.__new__(PortaSwitchAdapter)
    adapter._portaswitch_settings = PortaSwitchSettings()
    adapter._admin_api = admin
    adapter._account_api = account or AccountAPI()
    adapter._init_portaswitch_version_cache_state()
    return adapter


async def history(adapter):
    return await adapter.retrieve_calls(SessionInfo(user_id="1", access_token="token", refresh_token="refresh"), UserInfo(user_id="1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("version, by_registry", [
    ("MR120-3", False),
    ("MR128", False),
    ("MR128-9", False),
    ("MR129", True),
    ("MR129-0", True),
    ("MR131-2", True),
])
async def test_the_release_picks_the_detection(version, by_registry):
    adapter = make_adapter(AdminAPI(version))
    assert await adapter._recordings_by_registry() is by_registry


@pytest.mark.asyncio
async def test_before_mr129_one_request_and_bit_64_shows_a_recording():
    account = AccountAPI([_cdr(bit_flags=4 | 64), _cdr(i_xdr=129580144, bit_flags=4)])
    adapter = make_adapter(AdminAPI("MR120-3"), account)

    items, _ = await history(adapter)

    assert len(account.calls) == 1
    assert "with_cr_download_ids" not in account.calls[0]
    assert items[0].recording_id is not None
    assert items[1].recording_id is None


@pytest.mark.asyncio
async def test_from_mr129_the_recorded_calls_are_asked_for_and_bit_64_is_ignored():
    stale = _cdr(bit_flags=4 | 64, bill_time="2025-09-07 16:00:50")
    recorded = _cdr(i_xdr=129580144, bill_time="2025-09-07 16:00:44")
    account = AccountAPI([stale, recorded], recorded=[recorded])
    adapter = make_adapter(AdminAPI("MR129-1"), account)

    items, _ = await history(adapter)

    page_call, recorded_call = account.calls
    assert "call_recording" not in page_call and "with_cr_download_ids" not in page_call
    assert recorded_call["call_recording"] is True
    assert recorded_call["get_total"] is False
    assert recorded_call["page"] == 1
    assert items[0].recording_id is None
    assert items[1].recording_id is not None


@pytest.mark.asyncio
async def test_the_second_request_covers_the_page_up_to_its_newest_second():
    """to_date is exclusive: without the extra second the newest call of the page
    lost its recording (seen on a live switch, 39 of 40)."""
    newest = _cdr(i_xdr=3, bill_time="2025-09-07 16:00:50")
    middle = _cdr(i_xdr=2, bill_time="2025-09-07 16:00:47")
    oldest = _cdr(i_xdr=1, bill_time="2025-09-07 16:00:44")
    account = AccountAPI([newest, middle, oldest], recorded=[newest, oldest])
    adapter = make_adapter(AdminAPI("MR130-2"), account)

    items, _ = await history(adapter)

    recorded_call = account.calls[1]
    assert recorded_call["time_from"] == datetime(2025, 9, 7, 16, 0, 44)
    assert recorded_call["time_to"] == datetime(2025, 9, 7, 16, 0, 51)
    assert [item.recording_id is not None for item in items] == [True, False, True]


@pytest.mark.asyncio
async def test_a_later_page_gets_its_own_recordings_not_the_newest_ones():
    """The page's whole range with offset 0 would return the newest recorded calls
    of the range, and a later page would lose its recordings."""
    newer_page = [_cdr(i_xdr=100 + i, bill_time=f"2025-09-07 17:00:{i:02d}") for i in range(3)]
    this_page = [_cdr(i_xdr=10 + i, bill_time=f"2025-09-07 16:00:{i:02d}") for i in range(3)]
    account = AccountAPI(this_page, recorded=newer_page + this_page)
    adapter = make_adapter(AdminAPI("MR130-2"), account)

    items, _ = await adapter.retrieve_calls(
        SessionInfo(user_id="1", access_token="token", refresh_token="refresh"), UserInfo(user_id="1"),
        page=2, items_per_page=3,
    )

    assert all(item.recording_id is not None for item in items)


@pytest.mark.asyncio
async def test_an_empty_page_asks_nothing_more():
    account = AccountAPI([])
    adapter = make_adapter(AdminAPI("MR130-2"), account)

    await history(adapter)

    assert len(account.calls) == 1


def _timeout_error():
    """The shape async_http_api raises for a read timeout: no fault code to read."""
    return WebTritErrorException(
        408, "Request timeout", "timeout",
        bss_response_trace={"response_content": {}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_timeout_error(), RuntimeError("boom")])
async def test_a_failed_second_request_leaves_history_without_recordings(error):
    account = AccountAPI([_cdr()], recorded=[_cdr()], recorded_error=error)
    adapter = make_adapter(AdminAPI("MR130-2"), account)

    items, _ = await history(adapter)

    assert items[0].recording_id is None


@pytest.mark.asyncio
async def test_an_expired_session_on_the_second_request_still_answers_401():
    account = AccountAPI([_cdr()], recorded=[_cdr()], recorded_error=access_token_expired_error())
    adapter = make_adapter(AdminAPI("MR130-2"), account)

    with pytest.raises(WebTritErrorException) as raised:
        await history(adapter)

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_a_short_page_still_leaves_room_for_calls_of_the_same_second():
    account = AccountAPI([_cdr()], recorded=[_cdr()])
    adapter = make_adapter(AdminAPI("MR130-2"), account)

    await history(adapter)

    assert account.calls[1]["items_per_page"] == adapter_module.RECORDED_XDR_LOOKUP_MIN_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [
    AdminAPI(error=RuntimeError("switch down")),
    AdminAPI(version=None),
    AdminAPI(version="unknown"),
])
async def test_an_unreadable_version_asks_for_the_recorded_calls(admin):
    """Before MR129 call_recording=1 filters by bit 64, so it is correct on every
    release; history must not fail."""
    account = AccountAPI([_cdr()], recorded=[_cdr()])
    adapter = make_adapter(admin, account)

    items, _ = await history(adapter)

    assert account.calls[1]["call_recording"] is True
    assert items[0].recording_id is not None


@pytest.mark.asyncio
async def test_the_version_is_read_once_per_ttl():
    admin = AdminAPI("MR120-3")
    adapter = make_adapter(admin)

    with patch.object(adapter_module.time, "monotonic", return_value=1000.0):
        await history(adapter)
        await history(adapter)
    assert admin.calls == 1

    later = 1000.0 + adapter_module.PORTASWITCH_VERSION_CACHE_TTL
    with patch.object(adapter_module.time, "monotonic", return_value=later):
        await history(adapter)
    assert admin.calls == 2


@pytest.mark.asyncio
async def test_a_failed_read_is_retried_after_a_short_pause():
    admin = AdminAPI(error=RuntimeError("switch down"))
    adapter = make_adapter(admin)

    with patch.object(adapter_module.time, "monotonic", return_value=1000.0):
        await history(adapter)
        await history(adapter)
    assert admin.calls == 1

    admin.error = None
    later = 1000.0 + adapter_module.PORTASWITCH_VERSION_RETRY_AFTER
    with patch.object(adapter_module.time, "monotonic", return_value=later):
        assert await adapter._recordings_by_registry() is False
    assert admin.calls == 2
