"""Runtime configuration, read from environment variables.

Every setting has a safe development default. ``Settings.validate()`` refuses
to start a staging or production process with development-only settings
(SQLite, local keys, dev login, a weak session secret, and so on), so a
misconfigured deploy fails at boot rather than in front of learners.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

ENVIRONMENTS = ("development", "test", "staging", "production")


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return default if v is None or v == "" else int(v)


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    database_url: str = "sqlite:///./cognitive_twin.db"
    redis_url: str = ""
    session_secret: str = "dev-only-insecure-session-secret-change-me"  # noqa: S105 (refused outside dev)
    session_ttl_minutes: int = 480
    public_base_url: str = "http://127.0.0.1:8000"
    allowed_hosts: list[str] = field(default_factory=lambda: ["*"])
    trusted_proxy_hops: int = 0

    # OpenID Connect (institution SSO)
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    api_audience: str = ""
    claim_role: str = "ct_role"
    claim_birthdate: str = "birthdate"
    claim_adult: str = "ct_adult_verified"
    claim_age_band: str = "ct_age_band"
    claim_guardian: str = "ct_guardian_consent"
    claim_region: str = "ct_region"
    claim_cohorts: str = "ct_cohorts"
    claim_institution: str = "ct_institution"
    dev_login: bool = True
    # Class codes and usernames, for schools without single sign-on
    local_accounts: bool = True

    # Keys
    key_provider: str = "local"  # "local" (development only) or "aws-kms"
    local_key_dir: str = "./.keys"
    local_key_passphrase: str = "dev-only-key-passphrase"  # noqa: S105 (refused outside dev)
    aws_region: str = ""

    # Product rules for phase one
    consent_policy_version: str = "2026-11-pilot-v1"
    neural_enabled: bool = False
    demo_enabled: bool = True

    # Rate limits (requests per minute)
    rate_user_per_min: int = 300
    rate_ip_per_min: int = 600
    rate_auth_per_min: int = 20
    rate_credential_per_min: int = 10

    # Operations
    log_level: str = "INFO"
    policy_cache_seconds: int = 15

    @property
    def is_dev(self) -> bool:
        return self.env in ("development", "test")

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    @property
    def dev_login_enabled(self) -> bool:
        return self.dev_login and self.is_dev

    @staticmethod
    def _database_url_from_env(default: str) -> str:
        """DATABASE_URL, or one assembled from DB_* parts (how ECS injects secrets)."""
        if os.environ.get("DATABASE_URL"):
            return os.environ["DATABASE_URL"]
        host = os.environ.get("DB_HOST")
        if not host:
            return default
        from urllib.parse import quote
        user = quote(os.environ.get("DB_USER", ""), safe="")
        pw = quote(os.environ.get("DB_PASSWORD", ""), safe="")
        port = os.environ.get("DB_PORT", "5432")
        name = os.environ.get("DB_NAME", "cognitive_twin")
        ssl = os.environ.get("DB_SSLMODE", "require")
        return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{name}?sslmode={ssl}"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get("CT_ENV", "development").strip().lower()
        if env not in ENVIRONMENTS:
            raise ConfigError(f"CT_ENV must be one of {ENVIRONMENTS}, got {env!r}")
        d = cls()
        return cls(
            env=env,
            database_url=cls._database_url_from_env(d.database_url),
            redis_url=os.environ.get("REDIS_URL", ""),
            session_secret=os.environ.get("CT_SESSION_SECRET", d.session_secret),
            session_ttl_minutes=_int("CT_SESSION_TTL_MINUTES", d.session_ttl_minutes),
            public_base_url=os.environ.get("CT_PUBLIC_BASE_URL", d.public_base_url).rstrip("/"),
            allowed_hosts=_list("CT_ALLOWED_HOSTS", "*"),
            trusted_proxy_hops=_int("CT_TRUSTED_PROXY_HOPS", 0),
            oidc_issuer=os.environ.get("OIDC_ISSUER", "").rstrip("/"),
            oidc_client_id=os.environ.get("OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET", ""),
            oidc_scopes=os.environ.get("OIDC_SCOPES", d.oidc_scopes),
            api_audience=os.environ.get("CT_API_AUDIENCE", ""),
            claim_role=os.environ.get("CT_CLAIM_ROLE", d.claim_role),
            claim_birthdate=os.environ.get("CT_CLAIM_BIRTHDATE", d.claim_birthdate),
            claim_adult=os.environ.get("CT_CLAIM_ADULT", d.claim_adult),
            claim_age_band=os.environ.get("CT_CLAIM_AGE_BAND", d.claim_age_band),
            claim_guardian=os.environ.get("CT_CLAIM_GUARDIAN", d.claim_guardian),
            local_accounts=_bool("CT_LOCAL_ACCOUNTS", True),
            claim_region=os.environ.get("CT_CLAIM_REGION", d.claim_region),
            claim_cohorts=os.environ.get("CT_CLAIM_COHORTS", d.claim_cohorts),
            claim_institution=os.environ.get("CT_CLAIM_INSTITUTION", d.claim_institution),
            dev_login=_bool("CT_DEV_LOGIN", env in ("development", "test")),
            key_provider=os.environ.get("CT_KEY_PROVIDER", "local"),
            local_key_dir=os.environ.get("CT_LOCAL_KEY_DIR", d.local_key_dir),
            local_key_passphrase=os.environ.get("CT_LOCAL_KEY_PASSPHRASE",
                                                d.local_key_passphrase),
            aws_region=os.environ.get("AWS_REGION", ""),
            consent_policy_version=os.environ.get("CT_CONSENT_POLICY_VERSION",
                                                  d.consent_policy_version),
            neural_enabled=_bool("CT_NEURAL_ENABLED", False),
            demo_enabled=_bool("CT_DEMO_ENABLED", env in ("development", "test")),
            rate_user_per_min=_int("CT_RATE_USER_PER_MIN", d.rate_user_per_min),
            rate_ip_per_min=_int("CT_RATE_IP_PER_MIN", d.rate_ip_per_min),
            rate_auth_per_min=_int("CT_RATE_AUTH_PER_MIN", d.rate_auth_per_min),
            rate_credential_per_min=_int("CT_RATE_CREDENTIAL_PER_MIN",
                                         d.rate_credential_per_min),
            log_level=os.environ.get("CT_LOG_LEVEL", "INFO").upper(),
            policy_cache_seconds=_int("CT_POLICY_CACHE_SECONDS", 15),
        )

    def validate(self) -> Settings:
        problems: list[str] = []
        if self.neural_enabled:
            problems.append("CT_NEURAL_ENABLED must stay false in phase one (requirement R4.3)")
        if not self.is_dev:
            if self.database_url.startswith("sqlite"):
                problems.append("DATABASE_URL must point to PostgreSQL outside development")
            if self.key_provider == "local":
                problems.append("CT_KEY_PROVIDER=local is development-only; use aws-kms")
            if len(self.session_secret) < 32 or "dev-only" in self.session_secret:
                problems.append("CT_SESSION_SECRET must be a random secret of 32+ characters")
            if not self.oidc_enabled and not self.local_accounts:
                problems.append("no way to sign in: set OIDC_ISSUER and OIDC_CLIENT_ID, or "
                                "leave CT_LOCAL_ACCOUNTS on")
            if self.dev_login:
                problems.append("CT_DEV_LOGIN must be false outside development")
            if "*" in self.allowed_hosts:
                problems.append("CT_ALLOWED_HOSTS must list the real host names")
            if not self.public_base_url.startswith("https://"):
                problems.append("CT_PUBLIC_BASE_URL must use https")
            if not self.redis_url:
                problems.append("REDIS_URL is required so rate limits are shared across instances")
        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env().validate()
