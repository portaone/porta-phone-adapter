"""`directPresence` - the capability switch for Core's own app-to-app presence.

Unlike `conference` or `conversationMute`, this one is not advertising: Core requires
the entry to be present in `supported`, and without it a controller neither publishes
its own status nor reads anyone else's (WT-1834). PortaSwitch is not involved at all -
the transport is Core's PubSub - so the adapter's only job is to carry the switch.
"""
import os
import sys

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

# PortaSwitchSettings is instantiated at import time and its URL/credential fields are
# mandatory; supply throwaway values before importing the adapter.
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
    """Capabilities the real PortaSwitch adapter reports under this config."""
    subject = object.__new__(PortaSwitchAdapter)
    subject.config = AppConfig({"Capabilities": config})
    return set(subject.calculate_capabilities())


class TestDirectPresenceCapability:
    def test_the_wire_value_is_what_core_matches_on(self):
        # Core looks for this exact string in `supported`; a rename here silently
        # switches direct presence off for every tenant.
        assert Capabilities.direct_presence.value == "directPresence"

    def test_portaswitch_codes_for_it(self):
        assert Capabilities.direct_presence in PortaSwitchAdapter.CAPABILITIES

    def test_enabled_when_the_deployment_says_nothing(self):
        # Direct presence has been unconditionally on in Core since it existed, so an
        # upgrade that leaves CAPABILITIES_DIRECT_PRESENCE unset must not withdraw it.
        assert Capabilities.direct_presence in capabilities_of()

    def test_the_switch_turns_it_off(self):
        assert Capabilities.direct_presence not in capabilities_of(DIRECT_PRESENCE="0")

    def test_the_switch_is_independent_of_the_sip_transports(self):
        enabled = capabilities_of(DIRECT_PRESENCE="0", SIP_PRESENCE="1", SIP_DIALOGS="1")

        assert Capabilities.sip_presence in enabled
        assert Capabilities.sip_dialogs in enabled
        assert Capabilities.direct_presence not in enabled

    def test_it_has_no_parent_capability(self):
        # Nothing to depend on: the feature is Core's own and needs no PortaSwitch
        # counterpart, so the dependency filter must not strip it.
        assert Capabilities.direct_presence not in BSSAdapter.CAPABILITY_DEPENDENCIES

    def test_the_option_name_matches_the_env_var(self):
        # AppConfig derives the env var by upper-casing and "_"-joining the path, so the
        # key below is literally what CAPABILITIES_DIRECT_PRESENCE sets.
        option = BSSAdapter.CONFIG_CAPABILITIES_OPTIONS["DIRECT_PRESENCE"]

        assert option["option"] == Capabilities.direct_presence
        assert option["default"] is True
