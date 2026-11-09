"""Signing-key management (requirement R2).

Validator keys are ECDSA P-256 (ES256), which every major KMS and HSM
supports. Private keys live in a ``KeyProvider``:

* ``AwsKmsKeyProvider`` (staging and production): the private key never
  leaves AWS KMS; the service only calls ``kms:Sign`` and ``kms:GetPublicKey``.
* ``LocalKeyProvider`` (development and tests only): encrypted PEM files on
  disk. ``Settings.validate()`` refuses it outside development.

The *public* half of every key, active or retired, is stored in the
``signing_keys`` table with a key id and a per-validator version (R2.2).
Verification only ever uses that table, so credentials keep verifying after a
restart or a rotation, and even while KMS is unreachable (R2.3, R2.5).
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db.models import SigningKey, Validator, utcnow

KEY_SERVICE_ERRORS = Counter("ct_key_service_errors_total",
                             "Failures talking to the key-management service", ["operation"])


class KeyServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewKey:
    provider_ref: str
    public_key_pem: str


class KeyProvider(Protocol):
    name: str

    def create_key(self, label: str) -> NewKey: ...
    def describe(self, provider_ref: str) -> NewKey: ...
    def sign(self, provider_ref: str, message: bytes) -> bytes: ...


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class LocalKeyProvider:
    """Development only: encrypted PEM files in a local directory."""

    name = "local"

    def __init__(self, directory: str, passphrase: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.passphrase = passphrase.encode()

    def _path(self, ref: str) -> Path:
        if not ref.replace("-", "").isalnum():
            raise KeyServiceError("invalid key reference")
        return self.dir / f"{ref}.pem"

    def create_key(self, label: str) -> NewKey:
        key = ec.generate_private_key(ec.SECP256R1())
        ref = f"{label}-{secrets.token_hex(6)}"
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(self.passphrase))
        path = self._path(ref)
        path.write_bytes(pem)
        path.chmod(0o600)
        return NewKey(ref, _public_pem(key.public_key()))

    def _load(self, ref: str) -> ec.EllipticCurvePrivateKey:
        try:
            return serialization.load_pem_private_key(self._path(ref).read_bytes(),
                                                      self.passphrase)
        except (OSError, ValueError) as e:
            KEY_SERVICE_ERRORS.labels("load").inc()
            raise KeyServiceError(f"cannot load local key {ref}") from e

    def describe(self, provider_ref: str) -> NewKey:
        return NewKey(provider_ref, _public_pem(self._load(provider_ref).public_key()))

    def sign(self, provider_ref: str, message: bytes) -> bytes:
        return self._load(provider_ref).sign(message, ec.ECDSA(hashes.SHA256()))


class AwsKmsKeyProvider:
    """Keys are created in KMS by Terraform (ECC_NIST_P256, SIGN_VERIFY).

    The service role needs only ``kms:Sign`` and ``kms:GetPublicKey`` on them.
    """

    name = "aws-kms"

    def __init__(self, region: str, client=None):
        if client is None:
            try:
                import boto3  # optional dependency: pip install cognitive-twin[aws]
            except ImportError as e:  # pragma: no cover
                raise KeyServiceError("boto3 is required for CT_KEY_PROVIDER=aws-kms") from e
            client = boto3.client("kms", region_name=region or None)
        self.kms = client

    def create_key(self, label: str) -> NewKey:
        raise KeyServiceError("KMS keys are provisioned by Terraform; register them with "
                              "`cognitive-twin keys rotate --kms-key-arn ...`")

    def describe(self, provider_ref: str) -> NewKey:
        try:
            resp = self.kms.get_public_key(KeyId=provider_ref)
        except Exception as e:
            KEY_SERVICE_ERRORS.labels("get_public_key").inc()
            raise KeyServiceError("KMS GetPublicKey failed") from e
        if resp.get("KeySpec") != "ECC_NIST_P256" or resp.get("KeyUsage") != "SIGN_VERIFY":
            raise KeyServiceError("KMS key must be ECC_NIST_P256 with SIGN_VERIFY usage")
        pub = serialization.load_der_public_key(resp["PublicKey"])
        return NewKey(provider_ref, _public_pem(pub))

    def sign(self, provider_ref: str, message: bytes) -> bytes:
        try:
            resp = self.kms.sign(KeyId=provider_ref, Message=message, MessageType="RAW",
                                 SigningAlgorithm="ECDSA_SHA_256")
        except Exception as e:
            KEY_SERVICE_ERRORS.labels("sign").inc()
            raise KeyServiceError("KMS Sign failed") from e
        return resp["Signature"]


def _public_pem(pub) -> str:
    return pub.public_bytes(serialization.Encoding.PEM,
                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def provider_from_settings(settings: Settings) -> KeyProvider:
    if settings.key_provider == "aws-kms":
        return AwsKmsKeyProvider(settings.aws_region)
    if settings.key_provider == "local":
        return LocalKeyProvider(settings.local_key_dir, settings.local_key_passphrase)
    raise KeyServiceError(f"unknown key provider {settings.key_provider!r}")


def verify_signature(public_key_pem: str, signature: bytes, message: bytes) -> bool:
    pub = serialization.load_pem_public_key(public_key_pem.encode())
    try:
        pub.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


# --- registry in the database ----------------------------------------------------------

class KeyRegistry:
    def __init__(self, db: Session, provider: KeyProvider):
        self.db = db
        self.provider = provider

    def active_key(self, validator_id: str) -> SigningKey:
        key = self.db.scalar(select(SigningKey).where(SigningKey.validator_id == validator_id,
                                                      SigningKey.status == "active"))
        if key is None:
            raise KeyServiceError(f"validator {validator_id} has no active key")
        return key

    def get(self, kid: str) -> SigningKey | None:
        return self.db.get(SigningKey, kid)

    def rotate(self, validator_id: str, provider_ref: str | None = None) -> SigningKey:
        """Add a new active key version and retire the previous one (kept for verifying)."""
        if self.db.get(Validator, validator_id) is None:
            raise KeyServiceError(f"unknown validator {validator_id}")
        new = (self.provider.describe(provider_ref) if provider_ref
               else self.provider.create_key(validator_id))
        current = self.db.scalars(select(SigningKey).where(
            SigningKey.validator_id == validator_id)).all()
        version = 1 + max((k.version for k in current), default=0)
        for k in current:
            if k.status == "active":
                k.status, k.retired_at = "retired", utcnow()
        key = SigningKey(kid=f"{validator_id}-v{version}-{secrets.token_hex(4)}",
                         validator_id=validator_id, version=version,
                         provider=self.provider.name, provider_ref=new.provider_ref,
                         public_key_pem=new.public_key_pem, status="active")
        self.db.add(key)
        self.db.flush()
        return key

    def sign(self, key: SigningKey, message: bytes) -> bytes:
        return self.provider.sign(key.provider_ref, message)

    def published(self) -> list[dict]:
        keys = self.db.scalars(select(SigningKey).order_by(SigningKey.validator_id,
                                                           SigningKey.version)).all()
        return [{"kid": k.kid, "validator": k.validator_id, "version": k.version,
                 "alg": k.algorithm, "status": k.status, "public_key_pem": k.public_key_pem,
                 "created_at": k.created_at.isoformat() if k.created_at else None,
                 "retired_at": k.retired_at.isoformat() if k.retired_at else None}
                for k in keys]
