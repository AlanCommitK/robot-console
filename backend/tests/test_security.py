"""security.py 的单元测试：bcrypt、JWT（签发/校验/过期/篡改/算法）、角色、机器人 token 摘要。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.security import (
    BCRYPT_MAX_PASSWORD_BYTES,
    TokenClaims,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_robot_token,
    new_ws_ticket,
    parse_ws_ticket_payload,
    role_allowed,
    verify_password,
    verify_robot_token,
    ws_ticket_payload,
)

SECRET = "unit-test-secret-that-is-at-least-32-bytes-long"


# --------------------------------------------------------------------------- bcrypt


@pytest.fixture(scope="module")
def admin_hash() -> str:
    return hash_password("admin123")


def test_bcrypt_hash_verifies_correct_password(admin_hash: str) -> None:
    assert admin_hash.startswith("$2b$")
    assert verify_password("admin123", admin_hash)


def test_bcrypt_rejects_wrong_password(admin_hash: str) -> None:
    assert not verify_password("admin124", admin_hash)
    assert not verify_password("", admin_hash)


def test_bcrypt_salts_every_hash(admin_hash: str) -> None:
    assert hash_password("admin123") != admin_hash  # 随机盐：同一口令两次哈希结果不同


def test_bcrypt_password_length_limit(admin_hash: str) -> None:
    too_long = "a" * (BCRYPT_MAX_PASSWORD_BYTES + 1)
    with pytest.raises(ValueError):
        hash_password(too_long)
    assert not verify_password(too_long, admin_hash)


def test_bcrypt_malformed_hash_is_a_mismatch_not_a_crash() -> None:
    assert not verify_password("admin123", "not-a-bcrypt-hash")


# --------------------------------------------------------------------------- JWT


def test_jwt_round_trip() -> None:
    token, expires_at = create_access_token("admin", "admin", secret=SECRET, expire_minutes=5)
    claims = decode_access_token(token, secret=SECRET)
    assert claims.username == "admin"
    assert claims.role == "admin"
    assert claims.expires_at == expires_at
    assert expires_at.microsecond == 0
    assert timedelta(minutes=4) < expires_at - datetime.now(UTC) <= timedelta(minutes=5)


def test_jwt_claims_are_sub_role_exp() -> None:
    token, _ = create_access_token("viewer", "viewer", secret=SECRET, expire_minutes=5)
    payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert set(payload) == {"sub", "role", "exp"}
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


def test_jwt_expired_token_is_rejected() -> None:
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    token, _ = create_access_token(
        "admin", "admin", secret=SECRET, expire_minutes=1, now=long_ago
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token, secret=SECRET)


def test_jwt_wrong_secret_is_rejected() -> None:
    token, _ = create_access_token("admin", "admin", secret=SECRET, expire_minutes=5)
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(token, secret=SECRET + "-other")


def test_jwt_tampered_payload_is_rejected() -> None:
    token, _ = create_access_token("viewer", "viewer", secret=SECRET, expire_minutes=5)
    forged, _ = create_access_token(
        "viewer", "admin", secret="attacker-secret-32-bytes-long!!!!", expire_minutes=5
    )
    header, _, signature = token.split(".")
    forged_payload = forged.split(".")[1]
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(f"{header}.{forged_payload}.{signature}", secret=SECRET)


def test_jwt_alg_none_is_rejected() -> None:
    exp = datetime.now(UTC) + timedelta(minutes=5)
    unsigned = jwt.encode({"sub": "admin", "role": "admin", "exp": exp}, None, algorithm="none")
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(unsigned, secret=SECRET)


def test_jwt_without_valid_role_is_rejected() -> None:
    exp = datetime.now(UTC) + timedelta(minutes=5)
    for payload in ({"sub": "x", "exp": exp}, {"sub": "x", "role": "root", "exp": exp}):
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        with pytest.raises(jwt.InvalidTokenError):
            decode_access_token(token, secret=SECRET)


def test_jwt_without_exp_is_rejected() -> None:
    token = jwt.encode({"sub": "admin", "role": "admin"}, SECRET, algorithm="HS256")
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(token, secret=SECRET)


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token("not.a.jwt", secret=SECRET)


# --------------------------------------------------------------------------- 角色


def test_role_check() -> None:
    assert role_allowed("admin", ("admin",))
    assert not role_allowed("viewer", ("admin",))
    assert role_allowed("viewer", ("admin", "viewer"))


# --------------------------------------------------------------------------- 机器人 token


def test_robot_token_hash_is_sha256_hex() -> None:
    # NIST 公布的 SHA-256("abc") 测试向量
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert hash_robot_token("abc") == expected
    assert len(hash_robot_token("dev-token-robot-1")) == 64


def test_robot_token_verification() -> None:
    stored = hash_robot_token("dev-token-robot-1")
    assert verify_robot_token("dev-token-robot-1", stored)
    assert not verify_robot_token("dev-token-robot-2", stored)
    assert not verify_robot_token("", stored)


# --------------------------------------------------------------------------- 看板 WebSocket 票据


def test_ws_tickets_are_long_random_and_url_safe() -> None:
    tickets = {new_ws_ticket() for _ in range(100)}
    assert len(tickets) == 100
    for ticket in tickets:
        assert len(ticket) >= 43  # 32 字节 -> base64url 43 个字符
        assert all(c.isalnum() or c in "-_" for c in ticket)


def test_ws_ticket_payload_round_trip_keeps_jwt_expiry() -> None:
    token, expires_at = create_access_token("viewer", "viewer", secret=SECRET, expire_minutes=5)
    claims = decode_access_token(token, secret=SECRET)
    assert parse_ws_ticket_payload(ws_ticket_payload(claims)) == TokenClaims(
        username="viewer", role="viewer", expires_at=expires_at
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"sub": "admin", "role": "admin"}',
        '{"sub": "admin", "role": "root", "exp": 1}',
        '{"sub": "admin", "role": "admin", "exp": "soon"}',
    ],
)
def test_malformed_ws_ticket_payload_is_rejected(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_ws_ticket_payload(payload)
