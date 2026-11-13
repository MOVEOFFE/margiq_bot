import base64
import hashlib
import re
from cryptography.fernet import Fernet
from config import settings


def _build_fernet(raw_key: str) -> Fernet:
    """
    Гарантированно создает валидный Fernet-шифратор из любой строки:
    1) Сначала очищает стандартный 44-символьный ключ от кириллицы ('г') и пробелов.
    2) Если ключ в панели хостинга обрезан или поврежден — генерирует валидный
       32-байтный ключ через SHA-256, никогда не падая с ValueError.
    """
    cleaned = re.sub(r"[^A-Za-z0-9\-_=]", "", (raw_key or "").strip())

    # Вариант 1: если в ключе 43-44 валидных base64-символа (твой рабочий ключ)
    body = cleaned.rstrip("=")
    if len(body) >= 43:
        candidate = (body[-43:] + "=").encode("utf-8")
        try:
            return Fernet(candidate)
        except Exception:
            pass

    # Вариант 2: если строка все еще валидный Fernet-ключ
    try:
        padded = cleaned + ("=" * ((4 - len(cleaned) % 4) % 4))
        return Fernet(padded.encode("utf-8"))
    except Exception:
        pass

    # Вариант 3 (100% защита от падения): детерминированный ключ из SHA-256
    digest = hashlib.sha256((raw_key or "margiq_default_secret").encode("utf-8")).digest()
    fallback_key = base64.urlsafe_b64encode(digest)
    return Fernet(fallback_key)


_cipher = _build_fernet(settings.ENCRYPTION_KEY)


def encrypt_token(raw_token: str) -> str:
    """Шифрует токен и возвращает строку (url-safe base64)."""
    if not raw_token:
        return ""
    return _cipher.encrypt(raw_token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token) -> str:
    """Расшифровывает токен. Поддерживает и str, и bytes из БД."""
    if not encrypted_token:
        return ""
    if isinstance(encrypted_token, str):
        encrypted_token = encrypted_token.encode("utf-8")
    return _cipher.decrypt(encrypted_token).decode("utf-8")