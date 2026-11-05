"""Call history detects recordings by the PortaSwitch release (WT-2241).

From MR129 on, get_xdr_list no longer sets bit_flags bit 64 for a recorded call, so the
adapter asks for cr_download_ids (WT-1939). Before MR129 that flag makes the request
50-300 times slower, so there the adapter leaves it out and reads bit 64, as it did
before WT-1939.
"""
import os
import sys
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
from bss.types import SessionInfo, UserInfo  # noqa: E402


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
    def __init__(self, xdr_list=()):
        self.xdr_list = list(xdr_list)
        self.calls = []

    async def get_xdr_list(self, **kwargs):
        self.calls.append(kwargs)
        return {"xdr_list": self.xdr_list, "total": len(self.xdr_list)}


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
@pytest.mark.parametrize("version, by_ids", [
    ("MR120-3", False),
    ("MR128", False),
    ("MR128-9", False),
    ("MR129", True),
    ("MR129-0", True),
    ("MR131-2", True),
])
async def test_the_release_picks_the_detection(version, by_ids):
    adapter = make_adapter(AdminAPI(version))
    assert await adapter._recordings_by_cr_download_ids() is by_ids


@pytest.mark.asyncio
async def test_before_mr129_the_flag_is_not_sent_and_bit_64_shows_a_recording():
    account = AccountAPI([_cdr(bit_flags=4 | 64), _cdr(i_xdr=129580144, bit_flags=4)])
    adapter = make_adapter(AdminAPI("MR120-3"), account)

    items, _ = await history(adapter)

    assert account.calls[0]["with_cr_download_ids"] is False
    assert items[0].recording_id is not None
    assert items[1].recording_id is None


@pytest.mark.asyncio
async def test_from_mr129_the_flag_is_sent_and_bit_64_is_ignored():
    account = AccountAPI([
        _cdr(bit_flags=4 | 64),
        _cdr(i_xdr=129580144, cr_download_ids=["E0C5281F_E5B8480E_1D3207FE_019AFB34"]),
    ])
    adapter = make_adapter(AdminAPI("MR129-1"), account)

    items, _ = await history(adapter)

    assert account.calls[0]["with_cr_download_ids"] is True
    assert items[0].recording_id is None
    assert items[1].recording_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [
    AdminAPI(error=RuntimeError("switch down")),
    AdminAPI(version=None),
    AdminAPI(version="unknown"),
])
async def test_an_unreadable_version_falls_back_to_cr_download_ids(admin):
    """Correct on every release, only slow on old ones; history must not fail."""
    account = AccountAPI()
    adapter = make_adapter(admin, account)

    await history(adapter)

    assert account.calls[0]["with_cr_download_ids"] is True


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
        assert await adapter._recordings_by_cr_download_ids() is False
    assert admin.calls == 2
