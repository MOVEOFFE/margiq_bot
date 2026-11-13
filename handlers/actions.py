from typing import Dict, List, Any
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import AsyncSessionFactory
from models import Seller, Product
from security import decrypt_token
from services.wb_client import WBClient
from services.ozon_client import OzonClient
from services.notifier import Notifier
from utils import fmt_short_date, promo_status

router = Router()


async def _get_owned_seller(session, seller_id: int, user_id: int) -> Seller | None:
    return (await session.execute(
        select(Seller).options(selectinload(Seller.products)).where(
            Seller.id == seller_id,
            Seller.telegram_id == user_id
        )
    )).scalars().first()


async def build_wb_promo_candidates(wb_client: WBClient, promo_id: int, seller_products: Dict[str, Product]) -> List[Dict[str, Any]]:
    not_in_action = await wb_client.get_promotion_nomenclatures(promo_id, in_action=False)
    already_in_action = await wb_client.get_promotion_nomenclatures(promo_id, in_action=True)

    raw_noms = not_in_action + already_in_action
    if not raw_noms:
        return []

    goods = await wb_client.get_all_seller_goods()
    goods_map = {str(g["sku"]): g for g in goods}

    candidates = []
    seen_skus = set()

    for n in raw_noms:
        sku = str(n.get("id"))
        if sku in seen_skus:
            continue
        seen_skus.add(sku)

        plan_price = float(n.get("planPrice") or n.get("price") or 0)
        prod_db = seller_products.get(sku)
        g_info = goods_map.get(sku, {})

        curr_price = float(
            g_info.get("seller_price")
            or (prod_db.last_seller_price if prod_db else 0)
            or n.get("currentPrice")
            or 0
        )
        min_price = float(prod_db.min_price or 0.0) if prod_db else 0.0
        title = (prod_db.title if prod_db and prod_db.title else g_info.get("vendor_code")) or sku

        if plan_price > 0 or curr_price > 0:
            candidates.append({
                "sku": sku,
                "title": title,
                "current_price": curr_price,
                "plan_price": plan_price if plan_price > 0 else curr_price,
                "min_price": min_price,
                "in_action": bool(n.get("inAction", False))
            })
    return candidates


async def build_ozon_promo_candidates(ozon_client: OzonClient, action_id: int, seller_products: Dict[str, Product]) -> List[Dict[str, Any]]:
    raw_cands = await ozon_client.get_promotion_candidates(action_id)
    # Дополнительно строим индекс по артикулу продавца (title / offer_id)
    by_title = {p.title: p for p in seller_products.values() if p.title}

    candidates = []
    for c in raw_cands:
        pid = str(c.get("product_id") or c.get("id") or "")
        real_sku = str(c.get("real_sku") or pid)
        offer_id = str(c.get("offer_id") or "")

        prod_db = (
            seller_products.get(real_sku)
            or seller_products.get(pid)
            or (by_title.get(offer_id) if offer_id else None)
        )

        # Если в БД нашли товар, берем его реальный SKU и название (offer_id)
        final_sku = prod_db.sku if prod_db else real_sku
        title = offer_id or (prod_db.title if prod_db and prod_db.title else final_sku)

        curr_price = float(c.get("price") or 0)
        if curr_price <= 0 and prod_db and prod_db.last_seller_price:
            curr_price = float(prod_db.last_seller_price)

        plan_price = float(c.get("max_action_price") or c.get("action_price") or curr_price)
        min_price = float(prod_db.min_price or 0.0) if prod_db else 0.0
        stock_val = int(c.get("stock") or 100)
        in_action = bool(c.get("_in_action", False))

        candidates.append({
            "sku": final_sku,
            "product_id": pid,
            "title": title,
            "current_price": curr_price,
            "plan_price": plan_price,
            "min_price": min_price,
            "stock": stock_val,
            "in_action": in_action
        })
    return candidates


@router.message(Command("promos"))
@router.message(F.text.in_(["🔥 Акции", "🔥 Календарь акций"]))
async def cmd_promotions_menu(message: Message):
    wait_msg = await message.answer("⏳ Загружаем календарь активных и будущих акций WB и Ozon...")
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).where(Seller.telegram_id == message.from_user.id)
        )).scalars().all()

        active_sellers = [s for s in sellers if (s.wb_token_encrypted or s.ozon_api_key_encrypted)]
        if not active_sellers:
            await wait_msg.edit_text("⚠️ Сначала подключите магазин в разделе 🏪 **Мои магазины**.", parse_mode="Markdown")
            return

        multi_shop = len(active_sellers) > 1
        buttons = []
        lines = [
            "🔥 **Календарь и калькулятор акций**\n",
            "Нажмите на любую акцию ниже для анализа рентабельности:\n"
            "• 🟢 **Выгодно** — требуемая скидка ≤ 5% (и выше Стоп-цены)\n"
            "• 🟡 **Умеренно** — скидка 5–8% (и выше Стоп-цены)\n"
            "• 🔴 **Демпинг** — скидка > 8% или ниже Стоп-цены\n",
            "💡 **Обозначения на кнопках:**\n"
            "• 🟣 / 🔵 — маркетплейс (**Wildberries** / **Ozon**)\n"
            "• 🎯 — **Обычная акция** (доступен расчёт каждого SKU в CSV и вход в 1 клик)\n"
            "• ⚡️ `[Авто]` — **Автоакция WB** (сводка участия товаров в автоакции)\n"
        ]

        for s in active_sellers:
            shop_prefix = f"[{s.shop_name[:10]}] " if (multi_shop and s.shop_name) else ""
            if s.marketplace == "wb" and s.wb_token_encrypted:
                token = decrypt_token(s.wb_token_encrypted)
                wb = WBClient(token_prices=token)
                promos = await wb.get_promotions()
                if promos:
                    promos_sorted = sorted(promos[:12], key=lambda x: 1 if str(x.get("type") or "").lower() == "auto" else 0)
                    reg_cnt = sum(1 for p in promos_sorted if str(p.get("type") or "").lower() != "auto")
                    auto_cnt = len(promos_sorted) - reg_cnt
                    lines.append(f"🟣 **{s.shop_name} (WB):** `{len(promos_sorted)}` акций (🎯 с расчётом: `{reg_cnt}` | ⚡️ авто: `{auto_cnt}`)")

                    for pr in promos_sorted:
                        p_id = pr.get("id")
                        p_name = pr.get("name", "Акция WB")
                        clean_p_name = p_name.replace("(автоматические скидки)", "").replace("(автоакция)", "").strip()
                        start = fmt_short_date(pr.get("startDateTime") or pr.get("date_start"))
                        end = fmt_short_date(pr.get("endDateTime") or pr.get("date_end"))
                        status = promo_status(pr.get("startDateTime") or pr.get("date_start"), pr.get("endDateTime") or pr.get("date_end"))
                        is_wb_auto = str(pr.get("type") or "").lower() == "auto"

                        mode_ico = "⚡️" if is_wb_auto else "🎯"
                        type_tag = " [Авто]" if is_wb_auto else ""
                        name_limit = 15 if multi_shop else 23
                        label = f"🟣{mode_ico} {shop_prefix}{clean_p_name[:name_limit]} · {start}–{end} · {status}{type_tag}"[:64]
                        buttons.append([InlineKeyboardButton(
                            text=label,
                            callback_data=f"analyze_promo_wb_{s.id}_{p_id}"
                        )])
            elif s.marketplace == "ozon" and s.ozon_api_key_encrypted:
                key = decrypt_token(s.ozon_api_key_encrypted)
                ozon = OzonClient(s.ozon_client_id, key)
                promos = await ozon.get_promotions()
                if promos:
                    lines.append(f"🔵 **{s.shop_name} (Ozon):** доступно `{len(promos[:10])}` акций")
                    for pr in promos[:10]:
                        p_id = pr.get("id")
                        p_name = pr.get("title", "Акция Ozon")
                        start = fmt_short_date(pr.get("startDateTime") or pr.get("date_start"))
                        end = fmt_short_date(pr.get("endDateTime") or pr.get("date_end"))
                        status = promo_status(pr.get("startDateTime") or pr.get("date_start"), pr.get("endDateTime") or pr.get("date_end"))
                        name_limit = 17 if multi_shop else 25
                        label = f"🔵🎯 {shop_prefix}{p_name[:name_limit]} · {start}–{end} · {status}"[:64]
                        buttons.append([InlineKeyboardButton(
                            text=label,
                            callback_data=f"analyze_promo_ozon_{s.id}_{p_id}"
                        )])

    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if len(buttons) == 1:
        await wait_msg.edit_text("ℹ️ Сейчас нет доступных акций в календарях ваших магазинов.", reply_markup=kb)
        return

    await wait_msg.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("analyze_promo_"))
async def cb_analyze_promo(callback: CallbackQuery):
    parts = callback.data.split("_")
    mp = parts[2]
    seller_id = int(parts[3])
    promo_id = int(parts[4])

    await callback.answer("⏳ Загружаем данные акции...")
    notifier = Notifier(callback.bot)

    async with AsyncSessionFactory() as session:
        seller = await _get_owned_seller(session, seller_id, callback.from_user.id)

        if not seller:
            await callback.message.answer("⚠️ Магазин не найден.")
            return

        prod_map = {p.sku: p for p in seller.products}

        if mp == "wb" and seller.wb_token_encrypted:
            token = decrypt_token(seller.wb_token_encrypted)
            wb = WBClient(token_prices=token)
            promos = await wb.get_promotions()
            base_promo = next((p for p in promos if int(p.get("id", 0)) == promo_id), {})
            details = await wb.get_promotion_details(promo_id)

            merged_promo = {**base_promo, **(details or {})}
            if not merged_promo.get("id"):
                merged_promo["id"] = promo_id
            if not merged_promo.get("name"):
                merged_promo["name"] = base_promo.get("name") or f"Акция #{promo_id}"

            candidates = await build_wb_promo_candidates(wb, promo_id, prod_map)
            await notifier.send_promotion_alert(
                chat_id=callback.from_user.id,
                promo=merged_promo,
                candidates=candidates,
                seller_id=seller.id,
                marketplace="WB",
                shop_name=seller.shop_name
            )

        elif mp == "ozon" and seller.ozon_api_key_encrypted:
            key = decrypt_token(seller.ozon_api_key_encrypted)
            ozon = OzonClient(seller.ozon_client_id, key)
            promos = await ozon.get_promotions()
            details = next((p for p in promos if int(p.get("id", 0)) == promo_id), {"id": promo_id, "title": f"Акция #{promo_id}"})

            candidates = await build_ozon_promo_candidates(ozon, promo_id, prod_map)
            await notifier.send_promotion_alert(
                chat_id=callback.from_user.id,
                promo=details,
                candidates=candidates,
                seller_id=seller.id,
                marketplace="Ozon",
                shop_name=seller.shop_name
            )


@router.callback_query(F.data.startswith("promo_join_"))
async def cb_promo_join(callback: CallbackQuery):
    parts = callback.data.split("_")
    mode = parts[2]
    mp = parts[3]
    seller_id = int(parts[4])
    promo_id = int(parts[5])

    max_allowed_drop = 5.0 if mode == "prof" else (8.0 if mode == "mod" else 100.0)
    await callback.answer("⏳ Отправляем товары в акцию...")

    async with AsyncSessionFactory() as session:
        seller = await _get_owned_seller(session, seller_id, callback.from_user.id)

        if not seller:
            await callback.message.answer("⚠️ Магазин не найден.")
            return

        prod_map = {p.sku: p for p in seller.products}

        if mp == "wb" and seller.wb_token_encrypted:
            token = decrypt_token(seller.wb_token_encrypted)
            wb = WBClient(token_prices=token)
            candidates = await build_wb_promo_candidates(wb, promo_id, prod_map)

            selected_skus = []
            blocked_by_stop = 0
            for c in candidates:
                if c.get("in_action"):
                    continue
                curr_p = float(c["current_price"])
                plan_p = float(c["plan_price"])
                min_p = float(c.get("min_price") or 0.0)

                if min_p > 0 and plan_p < min_p:
                    blocked_by_stop += 1
                    continue

                drop_pct = ((curr_p - plan_p) / curr_p * 100) if curr_p > 0 else 0
                if mode == "all" or drop_pct <= max_allowed_drop:
                    selected_skus.append(int(c["sku"]))

            if not selected_skus:
                stop_info = f" (защищено Стоп-ценой: {blocked_by_stop} SKU)" if blocked_by_stop else ""
                await callback.message.answer(f"⚠️ Нет новых товаров (вне акции), подходящих под выбранный порог скидки и Стоп-цену{stop_info}.")
                return

            ok, msg = await wb.upload_to_promotion(promo_id, selected_skus, upload_now=True)
            if ok:
                await callback.message.edit_reply_markup(reply_markup=None)
                stop_note = f"\n🛑 Защищено Стоп-ценой (не добавлено): **{blocked_by_stop} SKU**." if blocked_by_stop else ""
                await callback.message.answer(
                    f"✅ **Товары успешно отправлены в акцию WB!**\n"
                    f"Добавлено новых артикулов: **{len(selected_skus)} SKU**.{stop_note}",
                    parse_mode="Markdown"
                )
            else:
                await callback.message.answer(f"⚠️ Ответ WB при загрузке в акцию:\n`{msg}`", parse_mode="Markdown")

        elif mp == "ozon" and seller.ozon_api_key_encrypted:
            key = decrypt_token(seller.ozon_api_key_encrypted)
            ozon = OzonClient(seller.ozon_client_id, key)
            candidates = await build_ozon_promo_candidates(ozon, promo_id, prod_map)

            to_activate = []
            blocked_by_stop = 0
            for c in candidates:
                if c.get("in_action"):
                    continue
                curr_p = float(c["current_price"])
                plan_p = float(c["plan_price"])
                min_p = float(c.get("min_price") or 0.0)

                if min_p > 0 and plan_p < min_p:
                    blocked_by_stop += 1
                    continue

                drop_pct = ((curr_p - plan_p) / curr_p * 100) if curr_p > 0 else 0
                if mode == "all" or drop_pct <= max_allowed_drop:
                    # Для активации в Ozon API нужен именно внутренний product_id
                    pid_for_api = int(c.get("product_id") or c["sku"])
                    to_activate.append({
                        "product_id": pid_for_api,
                        "action_price": plan_p,
                        "stock": int(c.get("stock") or 100)
                    })

            if not to_activate:
                stop_info = f" (защищено Стоп-ценой: {blocked_by_stop} SKU)" if blocked_by_stop else ""
                await callback.message.answer(f"⚠️ Нет новых товаров (вне акции), подходящих под выбранный порог скидки и Стоп-цену{stop_info}.")
                return

            ok, msg = await ozon.activate_promotion_products(promo_id, to_activate)
            if ok:
                await callback.message.edit_reply_markup(reply_markup=None)
                stop_note = f"\n🛑 Защищено Стоп-ценой: **{blocked_by_stop} SKU**." if blocked_by_stop else ""
                await callback.message.answer(
                    f"✅ **Товары успешно добавлены в акцию Ozon!**\n"
                    f"Активировано позиций: **{len(to_activate)} SKU**.{stop_note}",
                    parse_mode="Markdown"
                )
            else:
                await callback.message.answer(f"⚠️ Ошибка Ozon при добавлении в акцию:\n`{msg}`", parse_mode="Markdown")


@router.callback_query(F.data.startswith("promo_decline_"))
async def cb_promo_decline(callback: CallbackQuery):
    await callback.answer("Отказ от участия зафиксирован")
    old_text = callback.message.text or ""
    await callback.message.edit_text(
        old_text + "\n\n🚫 **Статус: Вы отказались от добавления новых товаров в эту акцию.**",
        reply_markup=None
    )


@router.callback_query(F.data == "back_to_promos_list")
async def cb_back_to_promos_list(callback: CallbackQuery):
    await callback.answer()
    await cmd_promotions_menu(callback.message)