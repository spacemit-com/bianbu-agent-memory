import base64
import json

import pytest

from bianbu_agent_sdk.memory.oauth2 import OAuth2Client, OAuth2Config, OAuth2Error


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


def test_authorization_code_flow_uses_documented_endpoints_and_auth() -> None:
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return Response(
            {
                "code": 200,
                "msg": "ok",
                "data": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 7200,
                },
            }
        )

    client = OAuth2Client(
        OAuth2Config(
            client_id="memory",
            client_secret="secret",
            redirect_uri="https://app.example/callback",
            scope="memory:write",
        ),
        opener=opener,
    )
    url, state = client.authorization_url(state="csrf")
    assert "oauth2/authorize" in url
    assert "state=csrf" in url
    assert state == "csrf"
    token = client.exchange_code("one-time-code")
    assert token.access_token == "access"
    assert token.refresh_token == "refresh"
    request, timeout = calls[0]
    assert request.full_url.endswith("/oauth2/token")
    assert timeout == 10.0
    assert request.get_header("Authorization") == (
        "Basic " + base64.b64encode(b"memory:secret").decode()
    )
    assert request.data is not None
    assert b"code=one-time-code" in request.data
    assert b"client_secret" not in request.data


def test_provider_business_error_is_not_treated_as_http_success() -> None:
    def opener(request, timeout):
        return Response({"code": 500, "msg": "invalid accessToken", "data": None})

    client = OAuth2Client(
        OAuth2Config(
            client_id="memory",
            redirect_uri="https://app.example/callback",
        ),
        opener=opener,
    )
    with pytest.raises(OAuth2Error, match="invalid accessToken"):
        client.userinfo("bad-token")


def test_oauth_base_url_must_be_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        OAuth2Config(
            base_url="http://developer.example/auth",
            client_id="memory",
            redirect_uri="https://app/callback",
        )
