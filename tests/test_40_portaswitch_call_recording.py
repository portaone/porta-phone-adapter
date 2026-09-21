import base64
import sys
import os
import types
import importlib.util
from urllib.parse import quote

import pytest

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

_ps_path = os.path.join(_app_path, 'bss', 'adapters', 'portaswitch')

# Register a stub package so relative imports inside serializer.py resolve correctly,
# without executing __init__.py (which loads PortaSwitchAdapter and requires env vars).
_ps_pkg = types.ModuleType('bss.adapters.portaswitch')
_ps_pkg.__path__ = [_ps_path]
_ps_pkg.__package__ = 'bss.adapters.portaswitch'
sys.modules['bss.adapters.portaswitch'] = _ps_pkg


def _load(name, filename):
    full = f'bss.adapters.portaswitch.{name}'
    spec = importlib.util.spec_from_file_location(full, os.path.join(_ps_path, filename))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = 'bss.adapters.portaswitch'
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_load('types', 'types.py')
_serializer = _load('serializer', 'serializer.py')
Serializer = _serializer.Serializer


def _make_cdr(**overrides) -> dict:
    """A voice XDR as Account/get_xdr_list returns it, with an outgoing direction."""
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


class TestCallRecordingExist:
    def test_non_empty_cr_download_ids_means_recording_exists(self):
        cdr = _make_cdr(cr_download_ids=["E0C5281F_E5B8480E_1D3207FE_019AFB34"])
        assert Serializer._call_recording_exist(cdr) is True

    def test_empty_cr_download_ids_means_no_recording(self):
        cdr = _make_cdr(cr_download_ids=[])
        assert Serializer._call_recording_exist(cdr) is False

    def test_absent_cr_download_ids_means_no_recording(self):
        # The field is missing entirely when the billing reports no recording for the XDR.
        cdr = _make_cdr()
        assert Serializer._call_recording_exist(cdr) is False

    def test_legacy_bit_flags_64_no_longer_signals_a_recording(self):
        # Since MR129 the bit is not authoritative and since MR131 it is not set at all;
        # relying on it is what made every recording invisible in the dialer (WT-1939).
        cdr = _make_cdr(bit_flags=4 | 64)
        assert Serializer._call_recording_exist(cdr) is False


class TestGetCdrInfoRecordingId:
    def test_recording_id_carries_both_billing_keys(self):
        cdr = _make_cdr(cr_download_ids=["062BDBD0 7366C3AD 6ED2EA7A 66D2F473_0"])
        # The audio is downloaded by i_xdr, the transcript by h323_conf_id, and nothing
        # in the PortaBilling API maps one to the other (WT-1963).
        recording_id = Serializer.get_cdr_info(cdr).recording_id.root
        assert Serializer.parse_recording_id(recording_id) == (
            "129580143", "062BDBD0 7366C3AD 6ED2EA7A 66D2F473"
        )

    def test_recording_id_is_url_safe(self):
        # Core interpolates this value into a URL path without escaping it, and
        # h323_conf_id contains spaces.
        cdr = _make_cdr(cr_download_ids=["062BDBD0 7366C3AD 6ED2EA7A 66D2F473_0"])
        recording_id = Serializer.get_cdr_info(cdr).recording_id.root
        assert quote(recording_id, safe="") == recording_id

    def test_recording_id_falls_back_to_the_xdr_id_without_a_conf_id(self):
        cdr = _make_cdr(cr_download_ids=["062BDBD0 7366C3AD 6ED2EA7A 66D2F473_0"])
        del cdr["h323_conf_id"]
        assert Serializer.get_cdr_info(cdr).recording_id.root == "129580143"

    def test_recording_id_is_none_without_a_recording(self):
        assert Serializer.get_cdr_info(_make_cdr()).recording_id is None

    def test_recording_id_is_none_for_a_stale_bit_flag(self):
        cdr = _make_cdr(bit_flags=4 | 64)
        assert Serializer.get_cdr_info(cdr).recording_id is None


class TestParseRecordingId:
    def test_round_trip(self):
        cdr = _make_cdr(cr_download_ids=["062BDBD0 7366C3AD 6ED2EA7A 66D2F473_0"])
        assert Serializer.parse_recording_id(Serializer.compose_recording_id(cdr)) == (
            "129580143", "062BDBD0 7366C3AD 6ED2EA7A 66D2F473"
        )

    def test_a_bare_number_is_a_pre_wt_1963_id(self):
        # It still downloads the audio; there is no key to ask for a transcript with.
        assert Serializer.parse_recording_id("129580143") == ("129580143", None)

    @pytest.mark.parametrize("recording_id", [
        "not base64 at all!",
        base64.urlsafe_b64encode(b"129580143").decode().rstrip("="),        # no separator
        base64.urlsafe_b64encode(b"129580143|").decode().rstrip("="),       # empty conf id
        base64.urlsafe_b64encode(b"|062BDBD0").decode().rstrip("="),        # no i_xdr
        base64.urlsafe_b64encode(b"abc|062BDBD0").decode().rstrip("="),     # i_xdr not a number
    ])
    def test_malformed_ids_are_rejected(self, recording_id):
        with pytest.raises(ValueError):
            Serializer.parse_recording_id(recording_id)
