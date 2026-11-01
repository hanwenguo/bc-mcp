"""OpenID Connect relying party used to log the ledger owner in (e.g. through Authelia)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx2
import jwt

from ..config import OIDCConfig

SAFE_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]


class LoginError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class OIDCClient:
    def __init__(self, cfg: OIDCConfig, redirect_uri: str, http: httpx2.AsyncClient | None = None):
        self.cfg = cfg
        self.redirect_uri = redirect_uri
        self.http = http or httpx2.AsyncClient(timeout=15)
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0
        self._jwks: dict[str, Any] | None = None

    async def metadata(self) -> dict[str, Any]:
        if self._metadata is None or time.time() - self._metadata_at > 3600:
            url = f"{self.cfg.issuer}/.well-known/openid-configuration"
            try:
                response = await self.http.get(url)
                response.raise_for_status()
            except httpx2.HTTPError as exc:
                raise LoginError(f"cannot fetch OpenID configuration from {url}: {exc}") from exc
            meta = response.json()
            if meta.get("issuer", "").rstrip("/") != self.cfg.issuer:
                raise LoginError(f"issuer mismatch: configured {self.cfg.issuer}, provider says {meta.get('issuer')}")
            self._metadata, self._metadata_at, self._jwks = meta, time.time(), None
        return self._metadata

    async def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.cfg.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def _signing_keys(self, refresh: bool = False) -> jwt.PyJWKSet:
        meta = await self.metadata()
        if self._jwks is None or refresh:
            response = await self.http.get(meta["jwks_uri"])
            response.raise_for_status()
            self._jwks = response.json()
        return jwt.PyJWKSet.from_dict(self._jwks)

    async def _verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise LoginError(f"malformed id_token: {exc}") from exc
        alg = header.get("alg")
        if alg not in SAFE_ALGS:
            raise LoginError(f"id_token uses unsupported algorithm {alg!r}")
        key = None
        for refresh in (False, True):
            keys = await self._signing_keys(refresh)
            key = next((k for k in keys.keys if not header.get("kid") or k.key_id == header.get("kid")), None)
            if key is not None:
                break
        if key is None:
            raise LoginError("no signing key matches the id_token")
        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=[alg],
                audience=self.cfg.client_id,
                issuer=(await self.metadata())["issuer"],
                options={"require": ["exp", "iat", "sub"]},
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            raise LoginError(f"invalid id_token: {exc}") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise LoginError("id_token nonce does not match")
        # OIDC Core 3.1.3.7: with several audiences, the token must have been issued to us.
        audiences = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
        if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != self.cfg.client_id:
            raise LoginError("id_token was issued to another client (azp)")
        return claims

    async def finish(self, code: str, code_verifier: str, nonce: str) -> dict[str, Any]:
        """Exchange the provider's code and return the user's verified claims."""
        try:
            return await self._finish(code, code_verifier, nonce)
        except (httpx2.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
            raise LoginError(f"login provider response was unusable: {type(exc).__name__}") from exc

    async def _finish(self, code: str, code_verifier: str, nonce: str) -> dict[str, Any]:
        meta = await self.metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
        }
        auth = None
        if self.cfg.token_endpoint_auth_method == "client_secret_basic":
            auth = httpx2.BasicAuth(self.cfg.client_id, self.cfg.client_secret)
        else:
            form |= {"client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret}
        try:
            response = await self.http.post(meta["token_endpoint"], data=form, auth=auth)
        except httpx2.HTTPError as exc:
            raise LoginError(f"token request failed: {exc}") from exc
        if response.status_code != 200:
            raise LoginError(f"token request failed ({response.status_code}): {response.text[:300]}")
        tokens = response.json()
        if "id_token" not in tokens:
            raise LoginError("provider returned no id_token; is the openid scope allowed for this client?")
        claims = await self._verify_id_token(tokens["id_token"], nonce)
        if meta.get("userinfo_endpoint") and tokens.get("access_token"):
            info = await self.http.get(
                meta["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            if info.status_code == 200 and info.headers.get("content-type", "").startswith("application/json"):
                userinfo = info.json()
                if userinfo.get("sub") == claims["sub"]:
                    claims = {**userinfo, **claims}
        return claims

    def username(self, claims: dict[str, Any]) -> str:
        return str(claims.get(self.cfg.username_claim) or claims["sub"])

    def allowed(self, claims: dict[str, Any]) -> bool:
        if self.username(claims) in self.cfg.allowed_users:
            return True
        groups = claims.get("groups") or []
        if isinstance(groups, str):
            groups = [groups]
        return any(group in self.cfg.allowed_groups for group in groups)
