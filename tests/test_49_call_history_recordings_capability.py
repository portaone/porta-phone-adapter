"""`recording_id` in the call history follows the `recordings` capability (WT-2048).

`GET /user/recordings/{recording_id}` answers 501 when `recordings` is off, so the
history must not hand out an id a client would offer as a player that cannot play.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

# main.py loads the configured adapter at import time; PortaSwitch needs only these. The
# submodule, not the package: test_40 replaces the package with a stub that lacks the class.
os.environ.setdefault("BSS_ADAPTER_MODULE", "bss.adapters.portaswitch.adapter")
os.environ.setdefault("BSS_ADAPTER_CLASS", "PortaSwitchAdapter")
os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

from fastapi.security import HTTPAuthorizationCredentials

import main
from bss.types import Capabilities, CDRInfo, ConnectStatus, Direction, SessionInfo


def _call(recording_id):
    return CDRInfo(
        caller="7774",
        callee="7773",
        connect_time=datetime(2026, 9, 30, tzinfo=timezone.utc),
        direction=Direction.outgoing,
        duration=39,
        recording_id=recording_id,
        status=ConnectStatus.accepted,
    )


class _StubAdapter:
    async def validate_session(self, access_token):
        return SessionInfo(user_id="1", access_token=access_token)

    async def retrieve_calls(self, session, user, **kwargs):
        return [_call("Mzc3Mjd8QTI3NkMwNEY"), _call(None)], 2

    def default_id_if_none(self, tenant_id):
        return tenant_id


async def _history(monkeypatch, capabilities):
    monkeypatch.setattr(main, "bss", _StubAdapter())
    monkeypatch.setattr(main, "bss_capabilities", capabilities)
    return await main.get_user_history_list(
        page=1,
        items_per_page=100,
        auth_data=HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"),
        x_webtrit_tenant_id=None,
    )


@pytest.mark.asyncio
async def test_recording_id_is_null_without_recordings(monkeypatch):
    history = await _history(monkeypatch, [Capabilities.callHistory])

    assert [call.recording_id for call in history.items] == [None, None]


@pytest.mark.asyncio
async def test_recording_id_is_kept_with_recordings(monkeypatch):
    history = await _history(monkeypatch, [Capabilities.callHistory, Capabilities.recordings])

    assert [call.recording_id and call.recording_id.root for call in history.items] == ["Mzc3Mjd8QTI3NkMwNEY", None]
