from datetime import datetime, timedelta
from sqlalchemy import Column, Integer, BigInteger, String, Boolean, DateTime, Float, ForeignKey, Text
from sqlalchemy.orm import relationship
from database import Base, engine
from config import settings


class UserAccount(Base):
    __tablename__ = "user_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)

    max_shops = Column(Integer, default=settings.TRIAL_MAX_SHOPS)
    subscription_until = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS))
    tariff = Column(String, default="trial")
    is_banned = Column(Boolean, default=False)

    notify_spp = Column(Boolean, default=True)
    notify_price = Column(Boolean, default=True)
    notify_promos = Column(Boolean, default=True)

    saved_promo = Column(String, nullable=True)
    expiry_warned_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Seller(Base):
    __tablename__ = "sellers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    marketplace = Column(String(32), nullable=False) # 'wb' или 'ozon'
    shop_name = Column(String(128), nullable=True)
    
    wb_token_encrypted = Column(Text, nullable=True)
    wb_stats_token_encrypted = Column(Text, nullable=True)
    ozon_client_id = Column(String(64), nullable=True)
    ozon_api_key_encrypted = Column(Text, nullable=True)

    # Связи
    products = relationship("Product", back_populates="seller", cascade="all, delete-orphan")
    settings = relationship("SellerSettings", back_populates="seller", uselist=False, cascade="all, delete-orphan")


class SellerSettings(Base):
    __tablename__ = "seller_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seller_id = Column(Integer, ForeignKey("sellers.id", ondelete="CASCADE"), unique=True, nullable=False)
    
    spp_delta_threshold = Column(Integer, default=1)
    price_delta_threshold = Column(Float, default=1.0)
    auto_join_promos = Column(Boolean, default=False)  # Настройка режима акций

    # Обратная связь с Seller
    seller = relationship("Seller", back_populates="settings")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seller_id = Column(Integer, ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False)
    marketplace = Column(String, nullable=False)
    sku = Column(String, index=True, nullable=False)
    title = Column(String, nullable=True)
    is_flagship = Column(Boolean, default=False)

    last_seller_price = Column(Float, default=0.0)
    last_client_price = Column(Float, default=0.0)
    last_wallet_price = Column(Float, default=0.0)
    last_spp = Column(Integer, default=0)
    min_price = Column(Float, default=0.0)

    seller = relationship("Seller", back_populates="products")


class PromotionAlert(Base):
    __tablename__ = "promotion_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seller_id = Column(Integer, ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False)
    marketplace = Column(String, nullable=False)
    promo_external_id = Column(String, nullable=False)
    title = Column(String, nullable=True)
    notified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String, unique=True, nullable=False)
    discount_percent = Column(Integer, default=10)
    max_uses = Column(Integer, default=100)
    used_count = Column(Integer, default=0)
    expires_at = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    yookassa_id = Column(String, nullable=True)
    purpose = Column(String, nullable=False)
    amount_rub = Column(Float, default=0.0)
    status = Column(String, default="pending")
    tariff = Column(String, nullable=True)
    extra_shops = Column(Integer, default=0)
    promo_code = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "sqlite":
            await conn.run_sync(_migrate_sqlite)


def _migrate_sqlite(sync_conn):
    def col_exists(table: str, col: str) -> bool:
        rows = sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == col for r in rows)

    additions = [
        ("user_accounts", "tariff", "VARCHAR DEFAULT 'trial'"),
        ("user_accounts", "notify_spp", "BOOLEAN DEFAULT 1"),
        ("user_accounts", "notify_price", "BOOLEAN DEFAULT 1"),
        ("user_accounts", "notify_promos", "BOOLEAN DEFAULT 1"),
        ("user_accounts", "saved_promo", "VARCHAR"),
        ("user_accounts", "expiry_warned_until", "DATETIME"),
        ("sellers", "wb_stats_token_encrypted", "VARCHAR"),
        ("products", "min_price", "FLOAT DEFAULT 0.0"),
        ("seller_settings", "auto_join_promos", "BOOLEAN DEFAULT 0"), # <--- Добавьте эту строчку
    ]
    for table, col, ddl in additions:
        if not col_exists(table, col):
            sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")