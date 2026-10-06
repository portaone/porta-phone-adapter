import os
import sys
from datetime import datetime, timedelta

import pytest

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

# PortaSwitchSettings is instantiated when the portaswitch package is imported and its
# URL/credential fields are mandatory; supply throwaway values before importing it.
os.environ.setdefault('PORTASWITCH_ADMIN_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ACCOUNT_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ADMIN_API_LOGIN', 'admin')
os.environ.setdefault('PORTASWITCH_ADMIN_API_TOKEN', 'token')
os.environ.setdefault('PORTASWITCH_SIP_SERVER_HOST', '1.2.3.4')

from bss.http_api import HTTPAPIConnectorWithLogin, OAuthSessionData
from bss.adapters.portaswitch.api.admin import AdminAPI
from bss.adapters.portaswitch.config import PortaSwitchSettings


def make_api():
    settings = PortaSwitchSettings(
        ADMIN_API_URL='https://pbx.example.com',
        ACCOUNT_API_URL='https://pbx.example.com',
        SIP_SERVER_HOST='1.2.3.4',
        ADMIN_API_LOGIN='admin',
        ADMIN_API_TOKEN='token',
    )
    api = AdminAPI(settings)
    api.refresh_calls = 0
    api.login_calls = 0

    # login/refresh are async on the async connector (WT-1720), so the fakes
    # must be coroutines — session_in_progress awaits them.
    async def fake_refresh(user=None, auth_session=None):
        api.refresh_calls += 1
        return OAuthSessionData(access_token='new', access_token_expires_at=datetime.now() + timedelta(seconds=900))

    async def fake_login(user=None):
        api.login_calls += 1
        return OAuthSessionData(access_token='new', access_token_expires_at=datetime.now() + timedelta(seconds=900))

    api.refresh = fake_refresh
    api.login = fake_login
    return api


class TestAdminSessionRefresh:
    def test_proactive_refresh_disabled_for_portaswitch(self):
        assert AdminAPI.REFRESH_TOKEN_IN_ADVANCE == 0

    def test_base_class_default_untouched(self):
        assert HTTPAPIConnectorWithLogin.REFRESH_TOKEN_IN_ADVANCE == 15

    @pytest.mark.asyncio
    async def test_valid_short_ttl_token_is_reused(self):
        """A live token with TTL below the old 15-min threshold (PortaSwitch
        expires_in=900) must be reused as is, without a new Session/login."""
        api = make_api()
        session = OAuthSessionData(
            access_token='current',
            access_token_expires_at=datetime.now() + timedelta(seconds=600),
            refresh_token='rt',
        )
        result = await api.session_in_progress(None, session)
        assert result is not None
        assert result.access_token == 'current'
        assert api.refresh_calls == 0
        assert api.login_calls == 0

    @pytest.mark.asyncio
    async def test_expired_token_still_relogins(self):
        api = make_api()
        session = OAuthSessionData(
            access_token='old',
            access_token_expires_at=datetime.now() - timedelta(seconds=1),
        )
        result = await api.session_in_progress(None, session)
        assert result is not None
        assert result.access_token == 'new'
        assert api.login_calls == 1

    def test_other_adapters_still_refresh_in_advance(self):
        class Dummy(HTTPAPIConnectorWithLogin):
            def __init__(self):
                super().__init__('https://x')
                self.refreshed = 0

            def login(self, user=None):
                return OAuthSessionData(access_token='new')

            def refresh(self, user=None, auth_session=None):
                self.refreshed += 1
                return OAuthSessionData(access_token='refreshed')

        d = Dummy()
        session = OAuthSessionData(
            access_token='current',
            access_token_expires_at=datetime.now() + timedelta(minutes=10),
            refresh_token='rt',
        )
        result = d.session_in_progress(None, session)
        assert result.access_token == 'refreshed'
        assert d.refreshed == 1
