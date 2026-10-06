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


class TestFirstEmail:
    """Unit tests for Serializer._first_email (WT-732)."""

    def test_single_email_returned_as_is(self):
        assert Serializer._first_email("user@example.com") == "user@example.com"

    def test_comma_separated_returns_first(self):
        assert Serializer._first_email("first@example.com,second@example.com") == "first@example.com"

    def test_comma_with_spaces_strips_result(self):
        assert Serializer._first_email("first@example.com, second@example.com") == "first@example.com"

    def test_none_returns_none(self):
        assert Serializer._first_email(None) is None

    def test_empty_string_returns_none(self):
        assert Serializer._first_email("") is None

    def test_whitespace_only_returns_none(self):
        assert Serializer._first_email("   ") is None

    def test_three_emails_returns_first(self):
        assert Serializer._first_email("a@x.com,b@x.com,c@x.com") == "a@x.com"


class TestGetEndUserEmail:
    """Verify get_end_user does not raise ValidationError with multiple emails."""

    def _base_account_info(self, email):
        return {
            "id": "1000",
            "i_account": 42,
            "extension_id": "100",
            "extension_name": "Test User",
            "firstname": "Test",
            "lastname": "User",
            "customer_name": "Acme",
            "email": email,
            "balance": 10.0,
            "billing_model": 1,
            "credit_limit": 100.0,
            "iso_4217": "USD",
            "h323_password": "secret",
            "is_active": 1,
            "time_zone_name": "UTC",
            "i_account_alias": None,
        }

    def _make_sip_server(self):
        from bss.types import SIPServer
        return SIPServer(host="sip.example.com", port=5060)

    def test_single_email_accepted(self):
        info = self._base_account_info("user@example.com")
        user = Serializer.get_end_user(info, [], self._make_sip_server(), False, False)
        assert user.email == "user@example.com"

    def test_multi_email_no_validation_error(self):
        """WT-732: must not raise ValidationError when PortaSwitch returns multiple emails."""
        info = self._base_account_info("first@example.com,second@example.com")
        user = Serializer.get_end_user(info, [], self._make_sip_server(), False, False)
        assert user.email == "first@example.com"

    def test_multi_email_with_spaces_no_validation_error(self):
        info = self._base_account_info("first@example.com, second@example.com")
        user = Serializer.get_end_user(info, [], self._make_sip_server(), False, False)
        assert user.email == "first@example.com"

    def test_null_email_accepted(self):
        info = self._base_account_info(None)
        user = Serializer.get_end_user(info, [], self._make_sip_server(), False, False)
        assert user.email is None


class TestGetContactInfoEmail:
    """Verify get_contact_info_by_account does not raise ValidationError with multiple emails."""

    def _base_account_info(self, email):
        return {
            "id": "1000",
            "i_account": 42,
            "extension_id": "100",
            "extension_name": "Test User",
            "firstname": "Test",
            "lastname": "User",
            "companyname": "Acme",
            "email": email,
            "sip_status": 1,
            "alias_list": [],
        }

    def test_single_email_accepted(self):
        info = self._base_account_info("user@example.com")
        contact = Serializer.get_contact_info_by_account(info, current_user=99)
        assert contact.email == "user@example.com"

    def test_multi_email_no_validation_error(self):
        """WT-732: must not raise ValidationError when PortaSwitch returns multiple emails."""
        info = self._base_account_info("first@example.com,second@example.com")
        contact = Serializer.get_contact_info_by_account(info, current_user=99)
        assert contact.email == "first@example.com"

    def test_null_email_accepted(self):
        info = self._base_account_info(None)
        contact = Serializer.get_contact_info_by_account(info, current_user=99)
        assert contact.email is None
