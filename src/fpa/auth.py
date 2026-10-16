"""Identity: verify the bearer token, take the person from one claim.

In production the tokens come from the company's identity provider over OIDC and are
checked against its published JWKS, the same way for Entra ID or Okta. Here, in dev
mode, the API plays the identity provider: it signs RS256 tokens with a key it
generates at startup and verifies them with exactly the same code path.

Authorization does not live in the token. The token says who you are; the grants in
the database say what you can see.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from fpa.config import Settings


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    person_id: str
    claims: dict


class DevIssuer:
    kid = "fpa-dev-1"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self._private.public_key()

    def jwks(self) -> dict:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.public_key, as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def issue(self, person_id: str, ttl_seconds: int = 8 * 3600, **overrides) -> str:
        now = int(time.time())
        claims = {
            "iss": self.settings.oidc_issuer,
            "aud": self.settings.oidc_audience,
            "sub": person_id,
            self.settings.identity_claim: person_id,
            "iat": now,
            "exp": now + ttl_seconds,
        }
        claims.update(overrides)
        return jwt.encode(claims, self._private, algorithm="RS256", headers={"kid": self.kid})


class Verifier:
    def __init__(self, settings: Settings, dev_issuer: DevIssuer | None = None):
        self.settings = settings
        self.dev_issuer = dev_issuer
        self._jwks = None
        if settings.auth_mode == "oidc":
            if not settings.oidc_jwks_url:
                raise ValueError("FPA_OIDC_JWKS_URL is required when FPA_AUTH_MODE=oidc")
            self._jwks = jwt.PyJWKClient(settings.oidc_jwks_url, cache_keys=True)
        elif dev_issuer is None:
            raise ValueError("dev mode needs a DevIssuer")

    def _key(self, token: str):
        if self._jwks is not None:
            return self._jwks.get_signing_key_from_jwt(token).key
        return self.dev_issuer.public_key

    def verify(self, token: str) -> Identity:
        try:
            claims = jwt.decode(
                token,
                self._key(token),
                algorithms=["RS256"],
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        person = claims.get(self.settings.identity_claim)
        if not person or not isinstance(person, str):
            raise AuthError(f"token has no {self.settings.identity_claim} claim")
        return Identity(person.lower(), claims)
