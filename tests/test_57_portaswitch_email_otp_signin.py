"""OTP sign-in started with the account's email instead of its id (WT-2212).

An identifier that no account has as its id and that looks
like an address is looked up in the accounts' email field. Exactly one open account
must carry it; with none or several no code is sent. PortaBilling's email filter is a
LIKE pattern, so the candidate rows are compared exactly, case-insensitively, against
each entry of a comma-separated email field.
"""

import os
import sys
import types

import pytest

_app_path = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _app_path)

os.environ.setdefault("PORTASWITCH_ADMIN_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ACCOUNT_API_URL", "https://pbx.example.com")
os.environ.setdefault("PORTASWITCH_ADMIN_API_LOGIN", "admin")
os.environ.setdefault("PORTASWITCH_ADMIN_API_TOKEN", "token")
os.environ.setdefault("PORTASWITCH_SIP_SERVER_HOST", "1.2.3.4")

from bss.adapters.portaswitch import adapter as adapter_module
from bss.adapters.portaswitch.adapter import PortaSwitchAdapter
from bss.types import UserInfo
from report_error import WebTritErrorException

ADDON = "WebTrit Mobile"
EMAIL = "john_doe@ifonica.example"


def account(i_account, email, addons=(ADDON,), **extra):
    return {
        "i_account": i_account,
        "id": f"38044{i_account}",
        "email": email,
        "assigned_addons": [{"name": name} for name in addons],
        **extra,
    }


class FakeAdminAPI:
    def __init__(self, accounts):
        # get_account_list leaves assigned_addons out, so the list rows lack them.
        self._accounts = {a["i_account"]: a for a in accounts}
        self.email_searches = []
        self.created_otps = []

    async def get_account_info(self, **params):
        if "i_account" in params:
            return {"account_info": self._accounts.get(int(params["i_account"]))}
        found = [a for a in self._accounts.values() if a["id"] == params.get("id")]
        return {"account_info": found[0] if found else None}

    async def find_accounts_by_email(self, email, limit, offset):
        self.email_searches.append((email, offset))
        needle = email.lower()
        rows = [
            {k: v for k, v in a.items() if k != "assigned_addons"}
            for a in self._accounts.values()
            if needle in (a["email"] or "").lower()
        ]
        # No total: the adapter asks for none (WT-2279).
        return {"account_list": rows[offset : offset + limit]}

    async def create_otp(self, i_account, delivery_channel):
        self.created_otps.append(i_account)
        return {"success": 1}

    def current_access_token(self):
        return None

    async def get_env_info(self):
        return {"email": "pbx@example.com"}


class FakeOTPStorage:
    def __init__(self):
        self.stored = []

    def store(self, otp_id, i_account, user_id, bss_token=None):
        self.stored.append((i_account, user_id))


def adapter(accounts, allowed_addons=(ADDON,)):
    subject = object.__new__(PortaSwitchAdapter)
    subject._init_env_email_cache_state()
    subject._admin_api = FakeAdminAPI(accounts)
    subject._otp_storage = FakeOTPStorage()
    subject._portaswitch_settings = types.SimpleNamespace(ALLOWED_ADDONS=list(allowed_addons), ADMIN_API_TOKEN="token")
    return subject


async def request_otp(subject, user_ref=EMAIL):
    return await subject.generate_otp(UserInfo(user_id=user_ref))


class TestEmailSignIn:
    @pytest.mark.asyncio
    async def test_the_one_account_with_the_email_gets_the_code(self):
        subject = adapter([account(101, EMAIL), account(102, "other@ifonica.example")])

        await request_otp(subject)

        assert subject._admin_api.created_otps == [101]
        assert subject._otp_storage.stored == [(101, EMAIL)]

    @pytest.mark.asyncio
    async def test_the_address_is_compared_without_case(self):
        subject = adapter([account(101, EMAIL)])

        await request_otp(subject, user_ref=" John_Doe@Ifonica.example ")

        assert subject._admin_api.created_otps == [101]

    @pytest.mark.asyncio
    async def test_any_entry_of_a_multi_address_field_matches(self):
        subject = adapter([account(101, f"billing@ifonica.example, {EMAIL}")])

        await request_otp(subject)

        assert subject._admin_api.created_otps == [101]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", [f"John Doe <{EMAIL}>", f"billing@ifonica.example, John < {EMAIL} >"])
    async def test_a_name_and_address_entry_matches(self, field):
        subject = adapter([account(101, field)])

        await request_otp(subject)

        assert subject._admin_api.created_otps == [101]

    @pytest.mark.asyncio
    async def test_the_addon_gate_reads_the_full_account_row(self):
        # The list row has no assigned_addons; a gate reading it would refuse this account.
        subject = adapter([account(101, EMAIL)])

        await request_otp(subject)

        assert subject._admin_api.created_otps == [101]

    @pytest.mark.asyncio
    async def test_without_the_addon_it_is_kept_out(self):
        subject = adapter([account(101, EMAIL, addons=())])

        with pytest.raises(WebTritErrorException) as error:
            await request_otp(subject)

        assert error.value.code == "addon_required"
        assert subject._admin_api.created_otps == []


class TestNoCodeIsSent:
    @pytest.mark.asyncio
    async def test_when_no_account_has_the_email(self):
        subject = adapter([account(101, "other@ifonica.example")])

        with pytest.raises(WebTritErrorException) as error:
            await request_otp(subject)

        assert error.value.status_code == 404
        assert subject._admin_api.created_otps == []

    @pytest.mark.asyncio
    async def test_when_several_accounts_have_the_email(self):
        subject = adapter([account(101, EMAIL), account(102, EMAIL.upper())])

        with pytest.raises(WebTritErrorException) as error:
            await request_otp(subject)

        assert error.value.status_code == 422
        assert error.value.code == "ambiguous_user_ref"
        assert subject._admin_api.created_otps == []

    @pytest.mark.asyncio
    async def test_like_wildcards_and_longer_addresses_do_not_count(self):
        # "_" is a LIKE wildcard and "%x%" a substring match: no row has the address.
        subject = adapter(
            [
                account(101, "johnxdoe@ifonica.example"),
                account(102, f"a{EMAIL}.ua"),
                account(103, f"John <a{EMAIL}>"),
            ]
        )

        with pytest.raises(WebTritErrorException) as error:
            await request_otp(subject)

        assert error.value.status_code == 404
        assert subject._admin_api.created_otps == []


class TestIdLookupComesFirst:
    @pytest.mark.asyncio
    async def test_an_account_id_wins_over_an_email_match(self):
        by_id = account(101, "someone@ifonica.example")
        by_id["id"] = EMAIL
        subject = adapter([by_id, account(102, EMAIL)])

        await request_otp(subject)

        assert subject._admin_api.created_otps == [101]
        assert subject._admin_api.email_searches == []

    @pytest.mark.asyncio
    async def test_an_identifier_that_is_no_address_is_not_searched_by_email(self):
        subject = adapter([account(101, EMAIL)])

        with pytest.raises(WebTritErrorException) as error:
            await request_otp(subject, user_ref="380441234567")

        assert error.value.status_code == 404
        assert subject._admin_api.email_searches == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("user_ref", ["%@%", "%@ifonica.example", "john@ifonica"])
    async def test_a_wildcard_or_dotless_identifier_is_not_searched(self, user_ref):
        subject = adapter([account(101, EMAIL)])

        with pytest.raises(WebTritErrorException) as error:
            await request_otp(subject, user_ref=user_ref)

        assert error.value.status_code == 404
        assert subject._admin_api.email_searches == []


class TestCandidatePages:
    """A substring search can return more candidates than one page holds."""

    @staticmethod
    def candidates(count):
        # Longer addresses that contain EMAIL: every one is a candidate, none a match.
        return [account(200 + n, f"x{n}{EMAIL}") for n in range(count)]

    @pytest.mark.asyncio
    async def test_a_match_past_the_first_page_is_found(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "EMAIL_LOOKUP_PAGE", 2)
        subject = adapter(self.candidates(4) + [account(101, EMAIL)])

        await request_otp(subject)

        assert subject._admin_api.created_otps == [101]
        assert [offset for _, offset in subject._admin_api.email_searches] == [0, 2, 4]

    @pytest.mark.asyncio
    async def test_a_full_last_page_is_followed_by_an_empty_one(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "EMAIL_LOOKUP_PAGE", 2)
        subject = adapter(self.candidates(3) + [account(101, EMAIL)])

        await request_otp(subject)

        assert subject._admin_api.created_otps == [101]
        assert [offset for _, offset in subject._admin_api.email_searches] == [0, 2, 4]

    @pytest.mark.asyncio
    async def test_a_second_match_on_a_later_page_refuses(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "EMAIL_LOOKUP_PAGE", 2)
        subject = adapter([account(101, EMAIL)] + self.candidates(3) + [account(102, EMAIL)])

        with pytest.raises(WebTritErrorException) as error:
            await request_otp(subject)

        assert error.value.code == "ambiguous_user_ref"
        assert subject._admin_api.created_otps == []

    @pytest.mark.asyncio
    async def test_reading_stops_at_the_second_match(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "EMAIL_LOOKUP_PAGE", 2)
        subject = adapter([account(101, EMAIL), account(102, EMAIL)] + self.candidates(4))

        with pytest.raises(WebTritErrorException):
            await request_otp(subject)

        assert [offset for _, offset in subject._admin_api.email_searches] == [0]

    @pytest.mark.asyncio
    async def test_too_many_candidates_refuse_instead_of_picking(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "EMAIL_LOOKUP_PAGE", 2)
        monkeypatch.setattr(adapter_module, "EMAIL_LOOKUP_MAX_ROWS", 4)
        subject = adapter([account(101, EMAIL)] + self.candidates(5))

        with pytest.raises(WebTritErrorException) as error:
            await request_otp(subject)

        assert error.value.code == "ambiguous_user_ref"
        assert subject._admin_api.created_otps == []


@pytest.mark.asyncio
async def test_the_search_asks_for_no_total():
    # Counting the candidates is a second scan of every account (WT-2279).
    from bss.adapters.portaswitch.api.admin import AdminAPI

    sent = []

    async def send_request(**request):
        sent.append(request)
        return {"account_list": []}

    api = object.__new__(AdminAPI)
    api._send_request = send_request

    await api.find_accounts_by_email(EMAIL, limit=1000, offset=0)

    assert sent[0]["params"]["get_total"] == 0
    assert sent[0]["params"]["email"] == f"%{EMAIL}%"


def test_the_apps_are_told_an_email_is_accepted():
    assert PortaSwitchAdapter.OTP_LOGIN_IDENTIFIERS == ("phone_number", "email")
