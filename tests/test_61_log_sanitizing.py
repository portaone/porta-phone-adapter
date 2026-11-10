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


def test_verify_otp_one_time_password_is_redacted():
    assert sanitize_data({"one_time_password": "123456"}) == {"one_time_password": "***"}
    assert "123456" not in sanitize_text("one_time_password=123456")


def test_keys_that_only_mention_a_password_stay_readable():
    reply = {
        "passwordless": True,
        "i_password_policy": 3,
        "password_expired": "Y",
        "is_password_set": 1,
        "password_lifetime": 90,
    }

    assert sanitize_data(reply) == reply
    assert sanitize_text("password_expired=Y&i_password_policy=3") == "password_expired=Y&i_password_policy=3"


def test_text_fallback_redacts_any_password_key():
    assert "s3cr3t" not in sanitize_text("login=ph1x100&h323_password=s3cr3t")


def test_emails_are_partially_masked():
    assert mask_email("a@x.io") == "***@x.io"
    assert mask_email("ab@x.io") == "***@x.io"
    assert mask_email("abc@x.io") == "a***@x.io"
    assert mask_email("abcdef@x.io") == "a****f@x.io"
    assert mask_email("volodymyr@portaone.com") == "v****r@portaone.com"
    assert mask_email("volodymyr.v@portaone.com") == "vo****.v@portaone.com"
    assert mask_email("to: ann@x.io, cd.efgh@y.co") == "to: a***@x.io, c****h@y.co"


def test_emails_in_a_reply_body_are_masked():
    body = json.dumps({"items": [{"email": "patrick@example.com", "first_name": "Patrick", "numbers": {"ext": "101"}}]})

    assert json.loads(sanitize_text(body)) == {
        "items": [{"email": "p****k@example.com", "first_name": "Patrick", "numbers": {"ext": "101"}}]
    }


def test_otp_code_is_redacted_only_in_the_verify_request():
    assert sanitize_data({"otp_id": "abc", "code": "123456"}) == {"otp_id": "abc", "code": "***"}
    assert sanitize_data({"code": "access_token_expired"}) == {"code": "access_token_expired"}


def test_pydantic_models_are_sanitized():
    data = CustomRequest(email="someone@example.com", api_password="x")

    assert sanitize_data(data) == {"email": "s****e@example.com", "api_password": "***"}


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


def test_validation_errors_carry_no_password(caplog):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel
    from request_trace import RouteWithLogging

    class Login(BaseModel):
        login: str
        password: str
        otp: str

    router = APIRouter(route_class=RouteWithLogging)

    @router.post("/session")
    async def session(body: Login):
        return {}

    app = FastAPI()
    app.include_router(router)

    with caplog.at_level("INFO"):
        response = TestClient(app).post("/session", json={"login": "john.smith@x.io", "password": "S3cr3t!"})

    assert response.status_code == 422
    assert "S3cr3t!" not in caplog.text
    assert "S3cr3t!" not in response.text
    assert "john.smith@x.io" not in caplog.text
