import base64
import logging
import uuid
import aiohttp

from config import settings

logger = logging.getLogger(__name__)

API_URL = "https://api.yookassa.ru/v3"


class YooKassa:
    def __init__(self):
        self.shop_id = (settings.YOOKASSA_SHOP_ID or "").strip()
        self.secret_key = (settings.YOOKASSA_SECRET_KEY or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.shop_id and self.secret_key)

    def _headers(self) -> dict:
        token = base64.b64encode(f"{self.shop_id}:{self.secret_key}".encode("utf-8")).decode("utf-8")
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Idempotence-Key": uuid.uuid4().hex,
        }

    async def create_payment(self, amount_rub: float, description: str, metadata: dict) -> dict:
        # Убедимся, что return_url ведет в бот
        return_url = f"https://t.me/{settings.BOT_USERNAME}" if hasattr(settings, "BOT_USERNAME") and settings.BOT_USERNAME else "https://t.me/telegram"
        
        payload = {
            "amount": {"value": f"{float(amount_rub):.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url, "locale": "ru_RU"},
            "description": description[:128],
            "metadata": metadata,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{API_URL}/payments", json=payload, headers=self._headers()) as resp:
                    data = await resp.json()
                    if resp.status not in (200, 201):
                        logger.error("ЮKassa create_payment: %s %s", resp.status, data)
                        return {"error": data.get("description") or f"HTTP {resp.status}"}
                    return data
        except Exception as e:
            logger.exception("ЮKassa create_payment network error")
            return {"error": f"Сетевая ошибка: {e}"}

    async def get_payment(self, payment_id: str) -> dict:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{API_URL}/payments/{payment_id}", headers=self._headers()) as resp:
                    if resp.status != 200:
                        return {}
                    return await resp.json()
        except Exception as e:
            logger.exception("ЮKassa get_payment network error")
            return {}