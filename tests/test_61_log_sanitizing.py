"""What the adapter writes to its log must carry no secrets and no full e-mails (WT-2211)."""
import json
import os
import sys
import time

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

from request_trace import mask_email, sanitize_data, sanitize_text
from bss.models import CustomRequest


def test_any_password_key_is_redacted():
    reply = {"account_info": {"h323_password": "uA~5fC", "password": "pw", "login": "ph1x100"}}

    assert sanitize_data(reply) == {"account_info": {"h323_password": "***", "password": "***", "login": "ph1x100"}}


def test_text_fallback_redacts_any_password_key():
    assert "s3cr3t" not in sanitize_text("login=ph1x100&h323_password=s3cr3t")


def test_emails_are_partially_masked():
    assert mask_email("volodymyr@portaone.com") == "vo****yr@portaone.com"
    assert mask_email("ann@portaone.com") == "a***@portaone.com"
    assert mask_email("to: a.b@x.io, cd.efgh@y.co") == "to: a***@x.io, cd****gh@y.co"


def test_emails_in_a_reply_body_are_masked():
    body = json.dumps({"items": [{"email": "patrick@example.com", "first_name": "Patrick", "numbers": {"ext": "101"}}]})

    assert json.loads(sanitize_text(body)) == {
        "items": [{"email": "pa****ck@example.com", "first_name": "Patrick", "numbers": {"ext": "101"}}]
    }


def test_otp_code_is_redacted_only_in_the_verify_request():
    assert sanitize_data({"otp_id": "abc", "code": "123456"}) == {"otp_id": "abc", "code": "***"}
    assert sanitize_data({"code": "access_token_expired"}) == {"code": "access_token_expired"}


def test_pydantic_models_are_sanitized():
    data = CustomRequest(email="someone@example.com", api_password="x")

    assert sanitize_data(data) == {"email": "so****ne@example.com", "api_password": "***"}


def test_sign_in_tokens_are_masked():
    masked = sanitize_data({"api_token": "0123456789abcdef0123", "config_token": "fedcba9876543210fedc"})

    assert masked == {"api_token": "012345***0123", "config_token": "fedcba***fedc"}


def test_sip_uris_are_not_taken_for_emails():
    assert mask_email("sip:101@pbx.example.com, sips:alice@pbx.example.com") == (
        "sip:101@pbx.example.com, sips:alice@pbx.example.com"
    )


def test_nested_models_are_sanitized():
    data = {"items": [CustomRequest(password="x")]}

    assert sanitize_data(data) == {"items": [{"password": "***"}]}


def test_long_bodies_are_sanitized_in_linear_time():
    started = time.monotonic()
    sanitize_text("a" * 200_000)
    sanitize_text(json.dumps({"blob": "a" * 200_000}))

    assert time.monotonic() - started < 1
