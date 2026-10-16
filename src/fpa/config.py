"""Settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _home() -> Path:
    if os.environ.get("FPA_HOME"):
        return Path(os.environ["FPA_HOME"])
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    owner_dsn: str
    app_dsn: str
    app_password: str
    home: Path
    budget_version: str
    auth_mode: str
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str
    identity_claim: str
    llm_provider: str
    llm_model: str

    @property
    def sql_dir(self) -> Path:
        return self.home / "sql"

    @property
    def semantic_dir(self) -> Path:
        return self.home / "semantic"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env = os.environ.get
    return Settings(
        owner_dsn=env("FPA_OWNER_DSN", "postgresql://fpa:fpa@localhost:5432/fpa"),
        app_dsn=env("FPA_APP_DSN", "postgresql://fpa_app:fpa_app@localhost:5432/fpa"),
        app_password=env("FPA_APP_PASSWORD", "fpa_app"),
        home=_home(),
        budget_version=env("FPA_BUDGET_VERSION", "FY26 Budget"),
        # dev: the API signs its own tokens so the stack runs offline.
        # oidc: tokens come from the company IdP and are checked against its JWKS.
        auth_mode=env("FPA_AUTH_MODE", "dev"),
        oidc_issuer=env("FPA_OIDC_ISSUER", "https://idp.example.com/fpa-dev"),
        oidc_audience=env("FPA_OIDC_AUDIENCE", "fpa-data-layer"),
        oidc_jwks_url=env("FPA_OIDC_JWKS_URL", ""),
        identity_claim=env("FPA_IDENTITY_CLAIM", "email"),
        # template: deterministic drafts, no network. anthropic: Claude drafts, same validator.
        llm_provider=env("FPA_LLM_PROVIDER", "template"),
        llm_model=env("FPA_LLM_MODEL", "claude-opus-5-5"),
    )
