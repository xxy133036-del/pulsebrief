from hashlib import sha256
from hmac import compare_digest

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pwdlib import PasswordHash

from app.config import get_settings

password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hasher.verify(password, hashed)


def sign_code(email: str, code: str) -> str:
    secret = get_settings().auth_secret
    return sha256(f"{secret}:{email.casefold()}:{code}".encode()).hexdigest()


def verify_code(email: str, code: str, code_hash: str) -> bool:
    return compare_digest(sign_code(email, code), code_hash)


def create_access_token(user_id: str) -> str:
    return _serializer().dumps({"sub": user_id, "scope": "app"})


def decode_access_token(token: str) -> str | None:
    try:
        payload = _serializer().loads(token, max_age=60 * 60 * 24 * 30)
    except (BadSignature, SignatureExpired):
        return None
    subject = payload.get("sub") if isinstance(payload, dict) else None
    return subject if isinstance(subject, str) else None


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().auth_secret, salt="daily-ai-news-session-v1")
