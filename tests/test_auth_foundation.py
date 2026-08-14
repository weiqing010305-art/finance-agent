import jwt
import pytest

from backend.auth.models import PrincipalContext
from backend.auth.passwords import hash_password, verify_password
from backend.auth.policy import require_capability
from backend.auth.tokens import TokenCodec, token_hash


def test_password_is_argon2_and_verifies_without_plaintext():
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert "correct horse" not in encoded
    assert verify_password(encoded, "correct horse battery staple") is True
    assert verify_password(encoded, "wrong password") is False


def test_fixed_role_matrix_fails_closed():
    viewer = PrincipalContext("u", "t", "viewer")
    require_capability(viewer, "resource.read")
    with pytest.raises(PermissionError, match="not found"):
        require_capability(viewer, "research.create")


def test_access_and_refresh_tokens_have_separate_security_properties():
    codec = TokenCodec("x" * 32)
    principal = PrincipalContext("user", "tenant", "member")
    pair = codec.issue(principal)
    assert codec.decode_access(pair.access_token) == principal
    assert pair.refresh_token not in pair.refresh_token_hash
    assert token_hash(pair.refresh_token) == pair.refresh_token_hash
    raw = jwt.decode(pair.access_token, "x" * 32, algorithms=["HS256"], issuer="finscope-local", audience="finscope-api")
    assert raw["type"] == "access" and raw["role"] == "member"
