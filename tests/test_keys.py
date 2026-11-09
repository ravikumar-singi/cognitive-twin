"""R2.1: KMS-backed signing (with a fake KMS client) and failure handling."""

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from prometheus_client import REGISTRY

from cognitive_twin.db.models import Validator
from cognitive_twin.db.session import create_all_for_tests, make_engine, session_factory
from cognitive_twin.keys import AwsKmsKeyProvider, KeyRegistry, KeyServiceError, verify_signature


class FakeKMS:
    def __init__(self):
        self.keys = {}
        self.fail = False

    def add(self, arn):
        self.keys[arn] = ec.generate_private_key(ec.SECP256R1())

    def get_public_key(self, KeyId):
        return {"KeySpec": "ECC_NIST_P256", "KeyUsage": "SIGN_VERIFY",
                "PublicKey": self.keys[KeyId].public_key().public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)}

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):
        assert MessageType == "RAW" and SigningAlgorithm == "ECDSA_SHA_256"
        if self.fail:
            raise RuntimeError("KMS unavailable")
        return {"Signature": self.keys[KeyId].sign(Message, ec.ECDSA(hashes.SHA256()))}


def _db(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/k.db")
    create_all_for_tests(eng)
    return session_factory(eng)()


def test_kms_keys_sign_and_rotate_without_breaking_old_signatures(tmp_path):
    kms = FakeKMS()
    kms.add("arn:k1")
    kms.add("arn:k2")
    db = _db(tmp_path)
    db.add(Validator(id="v1", name="V1", stake=1))
    reg = KeyRegistry(db, AwsKmsKeyProvider("eu-west-1", client=kms))
    k1 = reg.rotate("v1", "arn:k1")
    sig1 = reg.sign(k1, b"hello")
    k2 = reg.rotate("v1", "arn:k2")
    assert (k1.status, k2.status, k2.version) == ("retired", "active", 2)
    assert verify_signature(reg.get(k1.kid).public_key_pem, sig1, b"hello")
    assert not verify_signature(k2.public_key_pem, sig1, b"hello")


def test_kms_cannot_create_keys_itself(tmp_path):
    db = _db(tmp_path)
    db.add(Validator(id="v1", name="V1", stake=1))
    reg = KeyRegistry(db, AwsKmsKeyProvider("eu-west-1", client=FakeKMS()))
    try:
        reg.rotate("v1")
    except KeyServiceError as e:
        assert "Terraform" in str(e)
    else:
        raise AssertionError("expected KeyServiceError")


def test_kms_failure_is_counted_and_surfaces_as_key_service_error(tmp_path):
    kms = FakeKMS()
    kms.add("arn:k1")
    db = _db(tmp_path)
    db.add(Validator(id="v1", name="V1", stake=1))
    reg = KeyRegistry(db, AwsKmsKeyProvider("eu-west-1", client=kms))
    k = reg.rotate("v1", "arn:k1")
    before = REGISTRY.get_sample_value("ct_key_service_errors_total", {"operation": "sign"}) or 0
    kms.fail = True
    try:
        reg.sign(k, b"x")
    except KeyServiceError:
        pass
    else:
        raise AssertionError("expected KeyServiceError")
    after = REGISTRY.get_sample_value("ct_key_service_errors_total", {"operation": "sign"})
    assert after == before + 1
