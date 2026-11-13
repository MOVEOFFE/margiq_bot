from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Dict, Any


class Settings(BaseSettings):
    BOT_TOKEN: str
    BOT_USERNAME: str = "margiq_bot"  # Юзернейм твоего бота без @
    ENCRYPTION_KEY: str
    DB_PATH: str = "database.db"
    DATABASE_URL: str | None = None
    ADMIN_IDS: str = "590568180"

    TRIAL_DAYS: int = 7
    TRIAL_MAX_SHOPS: int = 1

    BASIC_PRICE_RUB: int = 990
    BASIC_DAYS: int = 31
    BASIC_MAX_SHOPS: int = 2

    BUSINESS_PRICE_RUB: int = 1990
    BUSINESS_DAYS: int = 31
    BUSINESS_MAX_SHOPS: int = 4

    EXTRA_SHOP_PRICE_RUB: int = 490

    YOOKASSA_SHOP_ID: str | None = None
    YOOKASSA_SECRET_KEY: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def admin_ids_list(self) -> List[int]:
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip().isdigit()]

    @property
    def tariffs(self) -> Dict[str, Dict[str, Any]]:
        return {
            "basic": {
                "name": "Basic",
                "days": self.BASIC_DAYS,
                "max_shops": self.BASIC_MAX_SHOPS,
                "price": self.BASIC_PRICE_RUB,
            },
            "business": {
                "name": "Business",
                "days": self.BUSINESS_DAYS,
                "max_shops": self.BUSINESS_MAX_SHOPS,
                "price": self.BUSINESS_PRICE_RUB,
            },
        }


settings = Settings()