import sys
import os
import types
import importlib.util

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
    def test_recording_id_is_the_xdr_id_when_a_recording_exists(self):
        cdr = _make_cdr(cr_download_ids=["E0C5281F_E5B8480E_1D3207FE_019AFB34"])
        # The recording is still downloaded by i_xdr - cr_download_ids only tells it exists.
        assert Serializer.get_cdr_info(cdr).recording_id.root == "129580143"

    def test_recording_id_is_none_without_a_recording(self):
        assert Serializer.get_cdr_info(_make_cdr()).recording_id is None

    def test_recording_id_is_none_for_a_stale_bit_flag(self):
        cdr = _make_cdr(bit_flags=4 | 64)
        assert Serializer.get_cdr_info(cdr).recording_id is None
