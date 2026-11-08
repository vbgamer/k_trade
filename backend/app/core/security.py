import datetime
import hashlib
import hmac
from typing import Optional, Tuple

import bcrypt
import jwt
from cryptography.fernet import Fernet

from app.core.config import settings

fernet = Fernet(settings.ENCRYPTION_KEY.encode())

# Fixed salt used by the original PBKDF2 scheme. Only kept to verify (and upgrade) old hashes.
_LEGACY_SALT = b"salt_options_saas_platform_key"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> Tuple[bool, bool]:
    """
    Returns (is_valid, needs_rehash). needs_rehash is True when the stored hash
    uses the legacy PBKDF2 scheme and should be replaced with bcrypt.
    """
    if stored_hash.startswith("$2"):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8")), False
        except ValueError:
            return False, False

    legacy = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _LEGACY_SALT, 100000).hex()
    ok = hmac.compare_digest(legacy, stored_hash)
    return ok, ok


def generate_jwt(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_jwt(token: str) -> Optional[str]:
    """Returns the user id from a valid token, or None."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def encrypt(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()
