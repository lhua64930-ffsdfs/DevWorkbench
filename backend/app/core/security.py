from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def _derive_demo_key(secret: str) -> bytes:
    # Fernet 需要 urlsafe base64 编码的 32 字节密钥。
    # Production supplies a cryptographically random, independent secret. This
    # SHA-256 adapter preserves ciphertext compatibility; it is not a password KDF.
    import base64
    import hashlib

    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def encrypt_token(token: str, secret: str) -> str:
    return Fernet(_derive_demo_key(secret)).encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted: str, secret: str) -> str | None:
    from app.core.config import settings
    keys = [secret, *[key.strip() for key in settings.token_encryption_previous_keys.split(",") if key.strip()]]
    try:
        return MultiFernet([Fernet(_derive_demo_key(key)) for key in keys]).decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
