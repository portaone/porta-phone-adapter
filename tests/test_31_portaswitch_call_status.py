import os
import sys

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

from bss.adapters.portaswitch.serializer import Serializer
from bss.models import ConnectStatus, Direction


class TestXdrToCallStatus:
    def test_outgoing_failed_cause1_returns_failed(self):
        assert Serializer._xdr_to_call_status(True, 1, Direction.outgoing) == ConnectStatus.failed

    def test_incoming_failed_cause1_returns_error(self):
        assert Serializer._xdr_to_call_status(True, 1, Direction.incoming) == ConnectStatus.error

    def test_failed_cause16_returns_declined(self):
        assert Serializer._xdr_to_call_status(True, 16, Direction.incoming) == ConnectStatus.declined
        assert Serializer._xdr_to_call_status(True, 16, Direction.outgoing) == ConnectStatus.declined

    def test_failed_cause19_returns_missed(self):
        assert Serializer._xdr_to_call_status(True, 19, Direction.incoming) == ConnectStatus.missed
        assert Serializer._xdr_to_call_status(True, 19, Direction.outgoing) == ConnectStatus.missed

    def test_failed_cause13_returns_completed_elsewhere(self):
        assert Serializer._xdr_to_call_status(True, 13, Direction.incoming) == ConnectStatus.completed_elsewhere
        assert Serializer._xdr_to_call_status(True, 13, Direction.outgoing) == ConnectStatus.completed_elsewhere

    def test_not_failed_cause16_returns_accepted(self):
        assert Serializer._xdr_to_call_status(False, 16, Direction.incoming) == ConnectStatus.accepted
        assert Serializer._xdr_to_call_status(False, 16, Direction.outgoing) == ConnectStatus.accepted

    def test_unknown_cause_returns_error(self):
        assert Serializer._xdr_to_call_status(True, 99, Direction.incoming) == ConnectStatus.error
        assert Serializer._xdr_to_call_status(False, 99, Direction.outgoing) == ConnectStatus.error


class TestParseCallStatus:
    def _make_cdr(self, failed: int, disconnect_cause, bit_flags: int = 0) -> dict:
        return {
            "failed": failed,
            "disconnect_cause": disconnect_cause,
            "bit_flags": bit_flags,
        }

    def test_accepted_call(self):
        cdr = self._make_cdr(failed=0, disconnect_cause=16)
        assert Serializer.parse_call_status(cdr) == ConnectStatus.accepted

    def test_declined_call(self):
        cdr = self._make_cdr(failed=1, disconnect_cause=16)
        assert Serializer.parse_call_status(cdr) == ConnectStatus.declined

    def test_missed_call(self):
        cdr = self._make_cdr(failed=1, disconnect_cause=19)
        assert Serializer.parse_call_status(cdr) == ConnectStatus.missed

    def test_completed_elsewhere(self):
        # call answered by another extension in a hunt group (disconnect_cause=13)
        cdr = self._make_cdr(failed=1, disconnect_cause=13)
        assert Serializer.parse_call_status(cdr) == ConnectStatus.completed_elsewhere

    def test_completed_elsewhere_string_cause(self):
        # disconnect_cause may come as string from the API
        cdr = self._make_cdr(failed=1, disconnect_cause="13")
        assert Serializer.parse_call_status(cdr) == ConnectStatus.completed_elsewhere

    def test_failed_outgoing_call(self):
        # bit_flags & 12 == 4 → outgoing direction
        cdr = self._make_cdr(failed=1, disconnect_cause=1, bit_flags=4)
        assert Serializer.parse_call_status(cdr) == ConnectStatus.failed

    def test_unknown_cause_returns_error(self):
        cdr = self._make_cdr(failed=1, disconnect_cause=99)
        assert Serializer.parse_call_status(cdr) == ConnectStatus.error
