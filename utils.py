from datetime import datetime


def csv_safe(value) -> str:
    """Защита от CSV-инъекций: ячейки, начинающиеся с '=', '+', '-', '@' или табуляции, экранируем апострофом."""
    s = "" if value is None else str(value)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def parse_dt(dt_str):
    """Парсит ISO-дату (наивно, в UTC — как и весь проект). Возвращает datetime или None."""
    if not dt_str:
        return None
    try:
        clean = str(dt_str).replace("Z", "").split(".")[0]
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def fmt_short_date(dt_str) -> str:
    """Короткий формат даты: 15.11."""
    dt = parse_dt(dt_str)
    return dt.strftime("%d.%m") if dt else "—"


def fmt_date(dt_str) -> str:
    """Полный формат даты: 15.11.2026."""
    dt = parse_dt(dt_str)
    return dt.strftime("%d.%m.%Y") if dt else "—"


def promo_status(start_str, end_str, now=None) -> str:
    """Статус акции: 🔥 идёт / ⏳ через N дн. / ✅ завершена / 📅."""
    now = now or datetime.utcnow()
    s = parse_dt(start_str)
    e = parse_dt(end_str)
    if s and e and s <= now <= e:
        return "🔥 идёт"
    if s and now < s:
        days = (s - now).days
        return f"⏳ через {days} дн." if days >= 1 else "⏳ сегодня"
    if e and now > e:
        return "✅ завершена"
    return "📅"

def release_memory() -> None:
   """
   Принудительно запускает сборщик мусора Python и возвращает
   неиспользуемые страницы памяти glibc обратно ОС Linux (Docker-контейнеру).
   """
   import gc
   import ctypes
   gc.collect()
   try:
       libc = ctypes.CDLL("libc.so.6")
       libc.malloc_trim(0)
   except Exception:
       pass