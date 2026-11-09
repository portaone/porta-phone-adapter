"""`sipMwi` - the switch for Core's `message-summary` subscription (WT-2224).

PortaSwitch sends the message waiting indication only to subscribers unless
MediaServer.mwi_backward_compatibility is on, so without the subscription Core gets
no NOTIFY and the new voicemail push never fires.
"""
import os
import sys

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

os.environ.setdefault('PORTASWITCH_ADMIN_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ACCOUNT_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ADMIN_API_LOGIN', 'admin')
os.environ.setdefault('PORTASWITCH_ADMIN_API_TOKEN', 'token')
os.environ.setdefault('PORTASWITCH_SIP_SERVER_HOST', '1.2.3.4')

from app_config import AppConfig
from bss.adapters import BSSAdapter
from bss.adapters.portaswitch.adapter import PortaSwitchAdapter
from bss.types import Capabilities


def capabilities_of(**config):
    subject = object.__new__(PortaSwitchAdapter)
    subject.config = AppConfig({"Capabilities": config})
    return set(subject.calculate_capabilities())


class TestSipMwiCapability:
    def test_the_wire_value_is_what_core_matches_on(self):
        assert Capabilities.sip_mwi.value == "sipMwi"

    def test_portaswitch_codes_for_it(self):
        assert Capabilities.sip_mwi in PortaSwitchAdapter.CAPABILITIES

    def test_on_by_default_with_voicemail(self):
        assert Capabilities.sip_mwi in capabilities_of(VOICEMAIL="1")

    def test_the_switch_turns_it_off(self):
        assert Capabilities.sip_mwi not in capabilities_of(VOICEMAIL="1", SIP_MWI="0")

    def test_dropped_without_voicemail(self):
        assert Capabilities.sip_mwi not in capabilities_of(VOICEMAIL="0", SIP_MWI="1")

    def test_the_option_name_matches_the_env_var(self):
        option = BSSAdapter.CONFIG_CAPABILITIES_OPTIONS["SIP_MWI"]

        assert option["option"] == Capabilities.sip_mwi
        assert option["default"] is True
