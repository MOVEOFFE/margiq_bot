import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import AsyncSessionFactory
from models import UserAccount, Seller, SellerSettings, Product, PromotionAlert, Payment
from security import decrypt_token
from services.wb_client import WBClient
from services.ozon_client import OzonClient
from services.notifier import Notifier
from services.payments import YooKassa
from utils import parse_dt, fmt_date

logger = logging.getLogger(__name__)

bg_check_lock = asyncio.Lock()
_active_user_checks: set[int] = set()
_token_warned = set()


def _clean_md(text: str | None) -> str:
    if not text:
        return ""
    return str(text).replace("_", " ").replace("*", "").replace("`", "").replace("[", "(").replace("]", ")")


async def _warn_token_error(bot: Bot, warn_key, telegram_id: int, shop_name: str, mp_label: str, err_msg: str):
    # Игнорируем временные ночные таймауты сети и техработы маркетплейсов (500/502/504),
    # отправляя алерт селлеру ТОЛЬКО при реальной ошибке авторизации токена (401 / 403)
    if "TEMP_ERROR" in str(err_msg) or "Проверьте интернет-соединение" in str(err_msg) or "Не удалось подключиться" in str(err_msg):
        logger.info("Пропуск алерта в TG для %s (%s): временная сетевая ошибка (%s)", shop_name, mp_label, err_msg)
        return

    if warn_key in _token_warned:
        return
    _token_warned.add(warn_key)
    clean_err = str(err_msg).replace("AUTH_ERROR:", "").strip()
    try:
        safe_shop = _clean_md(shop_name)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"⚠️ **Ошибка доступа: {mp_label}**\n"
                f"Магазин «{safe_shop}»: {clean_err}\n\n"
                f"Обновите токен в разделе 🏪 Мои магазины, иначе мониторинг не сможет получать цены."
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def _detect_active_promo_title(promos: list, mp: str = "wb") -> str | None:
    """Находит название текущей активной (в т.ч. авто-) акции маркетплейса."""
    if not promos:
        return None
    now_utc = datetime.utcnow()
    fallback_title = None
    for pr in promos:
        s_dt = parse_dt(pr.get("startDateTime") or pr.get("date_start"))
        e_dt = parse_dt(pr.get("endDateTime") or pr.get("date_end"))
        if s_dt and e_dt and s_dt <= now_utc <= e_dt:
            title = pr.get("name") or pr.get("title") or ""
            is_auto = (
                str(pr.get("type") or "").lower() == "auto"
                or "авто" in title.lower()
                or bool(pr.get("is_auto_action"))
            )
            if is_auto and title:
                return title
            if title and not fallback_title:
                fallback_title = title
    return fallback_title


async def is_subscription_active(session, telegram_id: int) -> bool:
    acc = (await session.execute(
        select(UserAccount).where(UserAccount.telegram_id == telegram_id)
    )).scalars().first()
    if not acc:
        return True
    if acc.is_banned:
        return False
    if acc.subscription_until and acc.subscription_until < datetime.utcnow():
        return False
    return True


async def check_prices(bot: Bot, flagship_only: bool = False, target_user_id: int | None = None) -> int:
    if target_user_id:
        if target_user_id in _active_user_checks:
            logger.info("Проверка для пользователя %s уже выполняется.", target_user_id)
            return -1
        _active_user_checks.add(target_user_id)
    else:
        if bg_check_lock.locked():
            logger.info("Фоновая проверка цен уже выполняется. Пропуск наслоения.")
            return -1

    try:
        if not target_user_id:
            await bg_check_lock.acquire()

        notifier = Notifier(bot)
        total_changes_detected = 0

        async with AsyncSessionFactory() as session:
            stmt = select(Seller).options(
                selectinload(Seller.settings),
                selectinload(Seller.products)
            )
            if target_user_id:
                stmt = stmt.where(Seller.telegram_id == target_user_id)

            sellers = (await session.execute(stmt)).scalars().all()

            for seller in sellers:
                if not await is_subscription_active(session, seller.telegram_id):
                    continue

                acc = (await session.execute(
                    select(UserAccount).where(UserAccount.telegram_id == seller.telegram_id)
                )).scalars().first()
                notify_spp = acc.notify_spp if acc else True
                notify_price = acc.notify_price if acc else True

                spp_threshold = seller.settings.spp_delta_threshold if seller.settings else 1
                price_threshold = seller.settings.price_delta_threshold if seller.settings else 1.0

                wb_token = decrypt_token(seller.wb_token_encrypted) if seller.wb_token_encrypted else None
                ozon_key = decrypt_token(seller.ozon_api_key_encrypted) if seller.ozon_api_key_encrypted else None

                wb_all = [p for p in seller.products if p.marketplace == "wb"]
                ozon_all = [p for p in seller.products if p.marketplace == "ozon"]

                wb_products = [p for p in wb_all if (not flagship_only or p.is_flagship or (p.last_spp or 0) == 0)]
                ozon_has_flagships = any(p.is_flagship for p in ozon_all)
                ozon_products = [
                    p for p in ozon_all
                    if (not flagship_only or not ozon_has_flagships or p.is_flagship or (p.last_seller_price or 0) == 0)
                ]

                # 1. WILDBERRIES
                if wb_products and wb_token:
                    skus = [p.sku for p in wb_products]
                    logger.info("Опрос WB %s (TG:%s): %s SKU...", seller.shop_name, seller.telegram_id, len(skus))

                    wb_api = WBClient(token_prices=wb_token)
                    is_valid, err_msg = await wb_api.check_token_rights()
                    if not is_valid:
                        logger.warning("WB %s: проблема с токеном: %s", seller.shop_name, err_msg)
                        await _warn_token_error(bot, seller.id, seller.telegram_id, seller.shop_name or "Магазин WB", "WB", err_msg)
                        continue

                    goods = await wb_api.get_all_seller_goods()
                    official_prices = {str(g["sku"]): float(g.get("seller_price", 0)) for g in goods}
                    storefront_data = await WBClient.fetch_client_prices(skus, official_prices)

                    market_data = {}
                    for sku in skus:
                        s_price = official_prices.get(sku, 0.0)
                        if sku in storefront_data:
                            market_data[sku] = storefront_data[sku]
                        elif s_price > 0:
                            market_data[sku] = {
                                "seller_price": s_price,
                                "client_price": s_price,
                                "wallet_price": float(int(s_price * 0.97 * 100)) / 100.0,
                                "spp": 0
                            }

                    changes = []
                    spp_found_count = len(storefront_data)
                    for prod in wb_products:
                        if prod.sku in market_data:
                            data = market_data[prod.sku]
                            new_spp = int(data["spp"])
                            new_seller_price = float(data["seller_price"])
                            new_client_price = float(data["client_price"])
                            new_wallet_price = float(data["wallet_price"])
                            min_p = float(prod.min_price or 0.0)

                            old_spp = int(prod.last_spp or 0)
                            old_seller_price = float(prod.last_seller_price or 0.0)
                            old_client_price = float(prod.last_client_price or 0.0)

                            # Если витрина в этом цикле не ответила, НЕ сбрасываем известную СПП в 0%,
                            # даже если изменилась цена продавца (например, при входе в автоакцию WB)
                            if prod.sku not in storefront_data and old_spp > 0 and new_seller_price > 0:
                                new_spp = old_spp
                                new_client_price = round(new_seller_price * (1.0 - new_spp / 100.0), 2)
                                new_wallet_price = float(int(new_client_price * 0.97 * 100)) / 100.0

                            prod.last_spp = new_spp
                            prod.last_seller_price = new_seller_price
                            prod.last_client_price = new_client_price
                            prod.last_wallet_price = new_wallet_price

                            spp_diff = abs(new_spp - old_spp)
                            price_diff = abs(new_seller_price - old_seller_price) if old_seller_price > 0 else 0.0

                            spp_changed = (spp_diff >= spp_threshold) and (prod.sku in storefront_data)
                            price_changed = (old_seller_price > 0) and (price_diff >= price_threshold)
                            below_stop = (min_p > 0 and 0 < new_seller_price < min_p) and (
                                old_seller_price == 0.0 or old_seller_price >= min_p or price_changed or target_user_id is not None
                            )

                            if spp_changed or price_changed or below_stop:
                                changes.append({
                                    "sku": str(prod.sku),
                                    "title": prod.title or prod.sku,
                                    "old_spp": old_spp,
                                    "new_spp": new_spp,
                                    "spp_changed": spp_changed,
                                    "old_seller_price": old_seller_price if old_seller_price > 0 else new_seller_price,
                                    "new_seller_price": new_seller_price,
                                    "seller_price_changed": price_changed,
                                    "old_client_price": old_client_price if old_client_price > 0 else new_client_price,
                                    "new_client_price": new_client_price,
                                    "new_wallet_price": new_wallet_price,
                                    "min_price": min_p,
                                    "below_stop": below_stop,
                                })

                    changes_to_send = [
                        c for c in changes
                        if (c.get("spp_changed") and notify_spp)
                        or (c.get("seller_price_changed") and notify_price)
                        or c.get("below_stop")
                    ]
                    if changes_to_send:
                        total_changes_detected += len(changes_to_send)
                        wb_active_promo = None
                        if any(c.get("seller_price_changed") and c["new_seller_price"] < c["old_seller_price"] for c in changes_to_send):
                            try:
                                wb_promos_now = await wb_api.get_promotions()
                                wb_active_promo = await _detect_active_promo_title(wb_promos_now, mp="wb")
                            except Exception:
                                pass

                        await notifier.send_price_change_summary(
                            chat_id=seller.telegram_id,
                            changes=changes_to_send,
                            shop_name=seller.shop_name or "Магазин WB",
                            marketplace="wb",
                            active_promo_title=wb_active_promo
                        )
                    logger.info("WB %s: завершено. Витрина получена для %s SKU, изменений: %s", seller.shop_name, spp_found_count, len(changes))

                # 2. OZON
                if ozon_products and seller.ozon_client_id and ozon_key:
                    skus = [p.sku for p in ozon_products]
                    logger.info("Опрос Ozon %s (TG:%s): %s SKU...", seller.shop_name, seller.telegram_id, len(skus))
                    ozon_client = OzonClient(seller.ozon_client_id, ozon_key)
                    ok, err_msg = await ozon_client.check_token_rights()
                    if not ok:
                        logger.warning("Ozon %s: проблема с ключом: %s", seller.shop_name, err_msg)
                        await _warn_token_error(bot, seller.id, seller.telegram_id, seller.shop_name or "Магазин Ozon", "Ozon", err_msg)
                        continue
                    ozon_changes = []
                    ozon_updated_count = 0

                    for i in range(0, len(ozon_products), 100):
                        batch_prods = ozon_products[i:i + 100]
                        batch_skus = [p.sku for p in batch_prods]
                        batch_offers = [p.title for p in batch_prods if p.title]
                        prices_data = await ozon_client.get_prices(batch_skus, offer_ids=batch_offers)

                        for prod in batch_prods:
                            pdata = (
                                prices_data.get(prod.sku)
                                or (prices_data.get(prod.title) if prod.title else None)
                            )
                            if pdata:
                                ozon_updated_count += 1
                                real_sku = pdata.get("real_sku")
                                if real_sku and real_sku != prod.sku:
                                    prod.sku = str(real_sku)

                                new_seller_p = float(pdata["seller_price"])
                                new_client_p = float(pdata["client_price"])
                                new_wallet_p = float(pdata.get("wallet_price") or new_client_p)
                                new_coinvest = int(pdata["coinvest"])
                                min_p = float(prod.min_price or 0.0)

                                old_seller_p = float(prod.last_seller_price or 0.0)
                                old_client_p = float(prod.last_client_price or 0.0)
                                old_coinvest = int(prod.last_spp or 0)

                                prod.last_seller_price = new_seller_p
                                prod.last_client_price = new_client_p
                                prod.last_wallet_price = new_wallet_p
                                prod.last_spp = new_coinvest

                                price_diff = abs(new_seller_p - old_seller_p) if old_seller_p > 0 else 0.0
                                spp_diff = abs(new_coinvest - old_coinvest)

                                price_changed = (old_seller_p > 0) and (price_diff >= price_threshold)
                                spp_changed = (old_seller_p > 0) and (spp_diff >= spp_threshold)
                                below_stop = (min_p > 0 and 0 < new_seller_p < min_p) and (
                                    old_seller_p == 0.0 or old_seller_p >= min_p or price_changed or target_user_id is not None
                                )

                                if price_changed or spp_changed or below_stop:
                                    ozon_changes.append({
                                        "sku": str(prod.sku),
                                        "title": prod.title or prod.sku,
                                        "old_spp": old_coinvest,
                                        "new_spp": new_coinvest,
                                        "spp_changed": spp_changed,
                                        "old_seller_price": old_seller_p if old_seller_p > 0 else new_seller_p,
                                        "new_seller_price": new_seller_p,
                                        "seller_price_changed": price_changed,
                                        "old_client_price": old_client_p if old_client_p > 0 else new_client_p,
                                        "new_client_price": new_client_p,
                                        "new_wallet_price": new_wallet_p,
                                        "min_price": min_p,
                                        "below_stop": below_stop,
                                    })

                    ozon_changes_to_send = [
                        c for c in ozon_changes
                        if (c.get("spp_changed") and notify_spp)
                        or (c.get("seller_price_changed") and notify_price)
                        or c.get("below_stop")
                    ]
                    if ozon_changes_to_send:
                        total_changes_detected += len(ozon_changes_to_send)
                        ozon_active_promo = None
                        if any(c.get("seller_price_changed") and c["new_seller_price"] < c["old_seller_price"] for c in ozon_changes_to_send):
                            try:
                                ozon_promos_now = await ozon_client.get_promotions()
                                ozon_active_promo = await _detect_active_promo_title(ozon_promos_now, mp="ozon")
                            except Exception:
                                pass

                        await notifier.send_price_change_summary(
                            chat_id=seller.telegram_id,
                            changes=ozon_changes_to_send,
                            shop_name=seller.shop_name or "Магазин Ozon",
                            marketplace="ozon",
                            active_promo_title=ozon_active_promo
                        )
                    logger.info("Ozon %s: завершено. Получены цены для %s SKU, изменений: %s", seller.shop_name, ozon_updated_count, len(ozon_changes))

            await session.commit()
        return total_changes_detected

    finally:
        if target_user_id:
            _active_user_checks.discard(target_user_id)
        elif bg_check_lock.locked():
            bg_check_lock.release()
        from utils import release_memory
        release_memory()


def _filter_auto_join_candidates(candidates: list, max_drop_pct: float = 8.0) -> list:
    eligible = []
    for c in candidates:
        if c.get("in_action"):
            continue
        curr_p = float(c.get("current_price") or 0.0)
        plan_p = float(c.get("plan_price") or 0.0)
        min_p = float(c.get("min_price") or 0.0)

        if curr_p <= 0 or plan_p <= 0:
            continue

        if min_p > 0 and plan_p < min_p:
            continue

        drop_pct = ((curr_p - plan_p) / curr_p) * 100.0
        if drop_pct <= max_drop_pct:
            eligible.append(c)
    return eligible


async def check_promotions_task(bot: Bot):
    from handlers.actions import build_wb_promo_candidates, build_ozon_promo_candidates

    notifier = Notifier(bot)
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).options(
                selectinload(Seller.products),
                selectinload(Seller.settings)
            )
        )).scalars().all()

        for seller in sellers:
            if not await is_subscription_active(session, seller.telegram_id):
                continue

            acc = (await session.execute(
                select(UserAccount).where(UserAccount.telegram_id == seller.telegram_id)
            )).scalars().first()
            if acc and not acc.notify_promos:
                continue

            prod_map = {p.sku: p for p in seller.products}
            auto_join = seller.settings.auto_join_promos if seller.settings else False

            if seller.marketplace == "wb" and seller.wb_token_encrypted:
                token = decrypt_token(seller.wb_token_encrypted)
                wb = WBClient(token_prices=token)
                promos = await wb.get_promotions()
                for pr in promos:
                    raw_id = pr.get("id")
                    if not raw_id:
                        continue
                    promo_ext_id = f"wb_{seller.id}_{raw_id}"
                    exists = (await session.execute(
                        select(PromotionAlert).where(
                            PromotionAlert.seller_id == seller.id,
                            PromotionAlert.promo_external_id == promo_ext_id
                        )
                    )).scalars().first()

                    if not exists:
                        details = await wb.get_promotion_details(int(raw_id)) or pr
                        candidates = await build_wb_promo_candidates(wb, int(raw_id), prod_map)
                        if not candidates:
                            continue

                        if auto_join:
                            safe_items = _filter_auto_join_candidates(candidates, max_drop_pct=8.0)
                            eligible_skus = [int(c["sku"]) for c in safe_items]
                            if eligible_skus:
                                ok, _ = await wb.upload_to_promotion(int(raw_id), eligible_skus, upload_now=True)
                                if ok:
                                    for c in safe_items:
                                        c["in_action"] = True

                        session.add(PromotionAlert(
                            seller_id=seller.id,
                            marketplace="wb",
                            promo_external_id=promo_ext_id,
                            title=pr.get("name", "Акция WB"),
                            notified=True
                        ))
                        await notifier.send_promotion_alert(
                            chat_id=seller.telegram_id,
                            promo=details,
                            candidates=candidates,
                            seller_id=seller.id,
                            marketplace="WB",
                            shop_name=seller.shop_name or "Магазин WB"
                        )

            elif seller.marketplace == "ozon" and seller.ozon_api_key_encrypted:
                key = decrypt_token(seller.ozon_api_key_encrypted)
                ozon = OzonClient(seller.ozon_client_id, key)
                promos = await ozon.get_promotions()
                for pr in promos:
                    raw_id = pr.get("id")
                    if not raw_id:
                        continue
                    promo_ext_id = f"ozon_{seller.id}_{raw_id}"
                    exists = (await session.execute(
                        select(PromotionAlert).where(
                            PromotionAlert.seller_id == seller.id,
                            PromotionAlert.promo_external_id == promo_ext_id
                        )
                    )).scalars().first()

                    if not exists:
                        candidates = await build_ozon_promo_candidates(ozon, int(raw_id), prod_map)
                        if not candidates:
                            continue

                        if auto_join:
                            safe_items = _filter_auto_join_candidates(candidates, max_drop_pct=8.0)
                            to_activate = [
                                {
                                    "product_id": int(c["sku"]),
                                    "action_price": float(c["plan_price"]),
                                    "stock": int(c.get("stock") or 100)
                                }
                                for c in safe_items
                            ]
                            if to_activate:
                                ok, _ = await ozon.activate_promotion_products(int(raw_id), to_activate)
                                if ok:
                                    for c in safe_items:
                                        c["in_action"] = True

                        session.add(PromotionAlert(
                            seller_id=seller.id,
                            marketplace="ozon",
                            promo_external_id=promo_ext_id,
                            title=pr.get("title", "Акция Ozon"),
                            notified=True
                        ))
                        await notifier.send_promotion_alert(
                            chat_id=seller.telegram_id,
                            promo=pr,
                            candidates=candidates,
                            seller_id=seller.id,
                            marketplace="Ozon",
                            shop_name=seller.shop_name or "Магазин Ozon"
                        )

        await session.commit()


PROMO_REMIND_HOURS = 24


async def check_promo_reminders_task(bot: Bot):
    now = datetime.utcnow()
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(select(Seller))).scalars().all()

        for seller in sellers:
            if not await is_subscription_active(session, seller.telegram_id):
                continue

            acc = (await session.execute(
                select(UserAccount).where(UserAccount.telegram_id == seller.telegram_id)
            )).scalars().first()
            if acc and not acc.notify_promos:
                continue

            mp_code = None
            promos = []

            if seller.marketplace == "wb" and seller.wb_token_encrypted:
                token = decrypt_token(seller.wb_token_encrypted)
                wb = WBClient(token_prices=token)
                promos = await wb.get_promotions()
                mp_code = "wb"
            elif seller.marketplace == "ozon" and seller.ozon_api_key_encrypted:
                key = decrypt_token(seller.ozon_api_key_encrypted)
                ozon = OzonClient(seller.ozon_client_id, key)
                promos = await ozon.get_promotions()
                mp_code = "ozon"

            if not mp_code or not promos:
                continue

            mp_emoji = "🟣" if mp_code == "wb" else "🔵"
            mp_label = "Wildberries" if mp_code == "wb" else "Ozon"
            safe_shop = _clean_md(seller.shop_name or f"Магазин {mp_label}")

            for pr in promos:
                raw_id = pr.get("id")
                if not raw_id:
                    continue
                start_raw = pr.get("startDateTime") or pr.get("date_start") or ""
                end_raw = pr.get("endDateTime") or pr.get("date_end") or ""
                start_dt = parse_dt(start_raw)
                if not start_dt:
                    continue
                hours_left = (start_dt - now).total_seconds() / 3600
                if hours_left <= 0 or hours_left > PROMO_REMIND_HOURS:
                    continue

                dedup = f"{mp_code}_{seller.id}_{raw_id}_reminder"
                exists = (await session.execute(
                    select(PromotionAlert).where(
                        PromotionAlert.seller_id == seller.id,
                        PromotionAlert.promo_external_id == dedup
                    )
                )).scalars().first()
                if exists:
                    continue

                name = _clean_md(pr.get("name") or pr.get("title") or "Акция")
                is_auto = str(pr.get("type") or "").lower() == "auto" or "автоакц" in name.lower()
                type_badge = "⚡️ Автоакция" if is_auto else "🎯 Обычная акция"

                start_str = fmt_date(start_raw)
                end_str = fmt_date(end_raw)
                period_str = f"`{start_str}` — `{end_str}`" if end_str != "—" else f"`{start_str}`"
                hours_txt = f"~{max(1, int(hours_left))} ч." if hours_left < 24 else f"{int(hours_left // 24)} дн."

                text = (
                    f"{mp_emoji} **{mp_label} | Магазин «{safe_shop}»**\n"
                    f"🔔 **Скоро старт акции (через {hours_txt})**\n\n"
                    f"📌 **{name}**\n"
                    f"⚙️ Тип: {type_badge}\n"
                    f"📅 Период: {period_str}\n\n"
                    f"Нажмите кнопку ниже, чтобы сразу проверить рентабельность товаров по магазину **{safe_shop}**:"
                )

                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"📊 Рассчитать для «{safe_shop[:18]}»",
                        callback_data=f"analyze_promo_{mp_code}_{seller.id}_{raw_id}"
                    )],
                    [InlineKeyboardButton(
                        text="🔥 Все акции календаря",
                        callback_data="back_to_promos_list"
                    )]
                ])

                try:
                    await bot.send_message(
                        chat_id=seller.telegram_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=kb
                    )
                except Exception:
                    continue

                session.add(PromotionAlert(
                    seller_id=seller.id,
                    marketplace=mp_code,
                    promo_external_id=dedup,
                    title=name,
                    notified=True
                ))

        await session.commit()


async def check_subscription_expiry_task(bot: Bot):
    now = datetime.utcnow()
    target_start = now
    target_end = now + timedelta(days=2)

    async with AsyncSessionFactory() as session:
        expiring = (await session.execute(
            select(UserAccount).where(
                UserAccount.subscription_until >= target_start,
                UserAccount.subscription_until <= target_end,
                UserAccount.is_banned == False,
                (UserAccount.expiry_warned_until.is_(None)) | (UserAccount.expiry_warned_until != UserAccount.subscription_until)
            )
        )).scalars().all()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Продлить тариф", callback_data="goto_tariffs")]
        ])

        for acc in expiring:
            until_str = acc.subscription_until.strftime("%d.%m.%Y в %H:%M")
            text = (
                f"⚠️ **Внимание: Ваша подписка в Margiq заканчивается через 2 дня!**\n\n"
                f"📅 Срок действия тарифа до: **{until_str} (UTC)**.\n"
                f"После окончания защита цен и уведомления об акциях будут приостановлены.\n\n"
                f"Продлите тариф, чтобы не потерять контроль над маржой:"
            )
            try:
                await bot.send_message(chat_id=acc.telegram_id, text=text, parse_mode="Markdown", reply_markup=kb)
                acc.expiry_warned_until = acc.subscription_until
            except Exception:
                pass

        await session.commit()


async def auto_update_flagships_task():
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(select(Seller))).scalars().all()
        for seller in sellers:
            if not seller.wb_token_encrypted:
                continue
            if not await is_subscription_active(session, seller.telegram_id):
                continue

            stats_token = decrypt_token(seller.wb_stats_token_encrypted) if seller.wb_stats_token_encrypted else decrypt_token(seller.wb_token_encrypted)
            client = WBClient(token_stats=stats_token)
            top_orders_map = await client.get_top_sales_skus(top_ratio=0.2)

            if top_orders_map:
                top_skus = list(top_orders_map.keys())
                await session.execute(
                    update(Product).where(Product.seller_id == seller.id).values(is_flagship=False)
                )
                await session.execute(
                    update(Product).where(Product.seller_id == seller.id, Product.sku.in_(top_skus)).values(is_flagship=True)
                )
                await session.commit()


async def poll_pending_payments_task(bot: Bot):
    from handlers.subscription import grant_purchase

    if not YooKassa().configured:
        return

    now = datetime.utcnow()
    async with AsyncSessionFactory() as session:
        pending = (await session.execute(select(Payment).where(Payment.status == "pending"))).scalars().all()

    yk = YooKassa()
    for p in pending:
        if p.created_at and (now - p.created_at).total_seconds() > 86400:
            async with AsyncSessionFactory() as session:
                p_old = (await session.execute(select(Payment).where(Payment.id == p.id))).scalars().first()
                if p_old:
                    p_old.status = "canceled"
                    await session.commit()
            continue

        data = await yk.get_payment(p.yookassa_id)
        status = data.get("status")
        if status == "succeeded":
            summary = await grant_purchase(p.id)
            try:
                await bot.send_message(p.telegram_id, f"🎉 **Оплата прошла успешно!**\n{summary}", parse_mode="Markdown")
            except Exception:
                pass
        elif status == "canceled":
            async with AsyncSessionFactory() as session:
                p2 = (await session.execute(select(Payment).where(Payment.id == p.id))).scalars().first()
                if p2:
                    p2.status = "canceled"
                    await session.commit()


async def backup_database_task():
    import os
    import sqlite3
    from database import engine, DATA_DIR

    if engine.dialect.name != "sqlite":
        return
    db_path = engine.url.database or ""
    if not db_path or not os.path.exists(db_path):
        return
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, f"database_{datetime.now().strftime('%Y-%m-%d')}.db")
    try:
        src = sqlite3.connect(db_path)
        try:
            bck = sqlite3.connect(dst)
            try:
                src.backup(bck)
            finally:
                bck.close()
        finally:
            src.close()
        logger.info("Бэкап БД создан: %s", dst)
    except Exception as e:
        logger.warning("Не удалось создать бэкап БД: %s", e)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone="Europe/Moscow",
        job_defaults={"max_instances": 1, "coalesce": True}
    )

    now_msk = datetime.now(ZoneInfo("Europe/Moscow"))

    scheduler.add_job(check_promotions_task, "interval", hours=6, args=[bot], id="check_promotions_task", next_run_time=now_msk)
    scheduler.add_job(check_promo_reminders_task, "interval", hours=6, args=[bot], id="check_promo_reminders_task", next_run_time=now_msk)
    scheduler.add_job(check_prices, "interval", minutes=15, args=[bot, True], id="check_flagship_prices")
    scheduler.add_job(check_prices, "interval", minutes=60, args=[bot, False], id="check_all_prices")
    scheduler.add_job(poll_pending_payments_task, "interval", minutes=5, args=[bot], id="poll_pending_payments_task")
    
    scheduler.add_job(
        check_subscription_expiry_task,
        "interval",
        hours=3,
        args=[bot],
        id="check_subscription_expiry"
    )
    scheduler.add_job(
        auto_update_flagships_task,
        CronTrigger(hour=3, minute=0, timezone="Europe/Moscow"),
        id="auto_update_flagships"
    )
    scheduler.add_job(
        backup_database_task,
        CronTrigger(hour=4, minute=0, timezone="Europe/Moscow"),
        id="backup_database_task"
    )
    return scheduler