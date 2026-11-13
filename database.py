import os
import sqlite3
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship
from config import settings

# Определяем постоянную директорию для Bothost (если есть /app, пишем туда, иначе локально)
DATA_DIR = os.getenv("DATA_DIR", "/app/data" if os.path.exists("/app") else ".")
os.makedirs(DATA_DIR, exist_ok=True)

# Имя файла базы данных для текущего проекта
DB_PATH = Path(DATA_DIR) / "database.db"

# Приоритет: явный DATABASE_URL (например PostgreSQL), иначе защищенный путь SQLite на Bothost
if getattr(settings, "DATABASE_URL", None):
    DB_URL = settings.DATABASE_URL
else:
    DB_URL = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"

engine = create_async_engine(DB_URL, echo=False, future=True)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)

Base = declarative_base()