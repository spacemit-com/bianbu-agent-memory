"""Small, dependency-free client for the Spacemit OAuth2 authorization code flow.

The daemon remains the authority for memory identity and never receives client
secrets through the memory protocol.  This module is intended for the Runtime or
BiBit login boundary: it exchanges codes/tokens over HTTPS and returns only the
authenticated account metadata needed to bind a Runtime identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import secrets
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


class OAuth2Error(RuntimeError):
    """An OAuth2 transport or provider-level error without token material."""


@dataclass(frozen=True, slots=True)
class OAuth2Config:
    base_url: str = "https://developer.spacemit.com/auth"
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scope: str = ""

    def __post_init__(self) -> None:
        base = self.base_url.rstrip("/") + "/"
        parsed = urlparse(base)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("OAuth2 base_url must be an HTTPS URL")
        if not self.client_id.strip() or not self.redirect_uri.strip():
            raise ValueError("OAuth2 client_id and redirect_uri are required")
        object.__setattr__(self, "base_url", base)


@dataclass(frozen=True, slots=True)
class OAuth2Token:
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    refresh_expires_in: int | None = None
    scope: str | None = None


def _provider_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    # The documented provider normally wraps responses in {code,msg,data};
    # gitlab/discourse-compatible clients may return the token map directly.
    if "code" in payload:
        if payload.get("code") != 200:
            raise OAuth2Error(str(payload.get("msg") or "OAuth2 provider rejected the request"))
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise OAuth2Error("OAuth2 provider returned an invalid data object")
        return data
    return payload


class OAuth2Client:
    def __init__(
        self,
        config: OAuth2Config,
        *,
        timeout: float = 10.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self._opener = opener

    def authorization_url(self, *, state: str | None = None) -> tuple[str, str]:
        selected_state = state or secrets.token_urlsafe(32)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": self.config.scope,
                "state": selected_state,
            }
        )
        return (
            urljoin(self.config.base_url, "oauth2/authorize") + "?" + query,
            selected_state,
        )

    def exchange_code(self, code: str) -> OAuth2Token:
        if not code.strip():
            raise ValueError("authorization code is required")
        payload = self._post_form(
            "oauth2/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
            },
            basic_auth=True,
        )
        return self._token(payload)

    def refresh(self, refresh_token: str) -> OAuth2Token:
        if not refresh_token.strip():
            raise ValueError("refresh token is required")
        payload = self._post_form(
            "oauth2/refresh",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            basic_auth=False,
        )
        return self._token(payload)

    def userinfo(self, access_token: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            urljoin(self.config.base_url, "oauth2/userinfo"),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        data = _provider_data(payload)
        if not data.get("id"):
            raise OAuth2Error("OAuth2 userinfo did not return a stable user id")
        return data

    def check_access_token(self, access_token: str) -> bool:
        payload = self._request_json(
            "GET",
            urljoin(self.config.base_url, "oauth2/checkAccessToken")
            + "?"
            + urlencode({"access_token": access_token}),
        )
        return payload.get("code") == 200

    def _token(self, payload: Mapping[str, Any]) -> OAuth2Token:
        data = _provider_data(payload)
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise OAuth2Error("OAuth2 provider returned no access token")
        return OAuth2Token(
            access_token=access_token,
            refresh_token=(str(data["refresh_token"]) if data.get("refresh_token") else None),
            expires_in=int(data["expires_in"]) if data.get("expires_in") is not None else None,
            refresh_expires_in=(
                int(data["refresh_expires_in"])
                if data.get("refresh_expires_in") is not None
                else None
            ),
            scope=str(data["scope"]) if data.get("scope") is not None else None,
        )

    def _post_form(
        self,
        endpoint: str,
        fields: Mapping[str, str],
        *,
        basic_auth: bool,
    ) -> Mapping[str, Any]:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        body = dict(fields)
        if basic_auth:
            credentials = base64.b64encode(
                f"{self.config.client_id}:{self.config.client_secret}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {credentials}"
        else:
            body.update(
                {
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                }
            )
        return self._request_json(
            "POST",
            urljoin(self.config.base_url, endpoint),
            headers=headers,
            body=urlencode(body).encode(),
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Mapping[str, Any]:
        request = Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise OAuth2Error(f"OAuth2 request failed: {type(exc).__name__}") from exc
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise OAuth2Error("OAuth2 provider returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise OAuth2Error("OAuth2 provider returned an invalid response")
        return payload


__all__ = ["OAuth2Client", "OAuth2Config", "OAuth2Error", "OAuth2Token"]
