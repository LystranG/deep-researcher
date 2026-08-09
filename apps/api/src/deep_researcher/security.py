import hashlib
import hmac
import secrets


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    password_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=password_salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
    )
    return f"scrypt${password_salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    algorithm, salt_hex, _ = encoded.split("$", maxsplit=2)
    if algorithm != "scrypt":
        return False
    expected = hash_password(password, salt=bytes.fromhex(salt_hex))
    return hmac.compare_digest(expected, encoded)


def issue_access_token() -> str:
    return secrets.token_urlsafe(32)


def hash_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
