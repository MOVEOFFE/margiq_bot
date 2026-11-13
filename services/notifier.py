import io
import csv
from datetime import datetime
from collections import Counter
from typing import List, Dict, Any
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton

from utils import csv_safe


def _clean_md(text: str | None) -> str:
    """Экранирует спецсимволы, чтобы не ломался parse_mode='Markdown'."""
    if not text:
        return ""
    return str(text).replace("_", " ").replace("*", "").replace("`", "").replace("[", "(").replace("]", ")")


def _plural_goods(n: int) -> str:
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 10 < n_abs < 20:
        return f"{n} товаров"
    if 1 < n1 < 5:
        return f"{n} товара"
    if n1 == 1:
        return f"{n} товар"
    return f"{n} товаров"


def _format_date(dt_str: str) -> str:
    if not dt_str:
        return "—"
    try:
        clean = dt_str.replace("Z", "").split(".")[0]
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return dt_str[:10]


class Notifier:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_price_change_summary(
        self,
        chat_id: int,
        changes: List[Dict[str, Any]],
        shop_name: str = "Мой магазин",
        marketplace: str = "wb",
        active_promo_title: str | None = None
    ):
        if not changes:
            return

        is_wb = marketplace.lower() == "wb"
        mp_emoji = "🟣" if is_wb else "🔵"
        mp_label = "Wildberries" if is_wb else "Ozon"
        mp_short = "WB" if is_wb else "Ozon"
        discount_label = "СПП" if is_wb else "Соинвест Ozon"
        wallet_col_name = (
            "Цена клиента с WB Кошельком (Стало, ₽)"
            if is_wb
            else "Цена клиента с Ozon Кошельком / Картой (Стало, ₽)"
        )
        safe_shop = _clean_md(shop_name)

        spp_transitions = Counter()
        price_up_count = 0
        price_down_items = []
        below_stop_items = []

        for ch in changes:
            old_spp = int(ch.get("old_spp", 0))
            new_spp = int(ch.get("new_spp", 0))
            if ch.get("spp_changed") and old_spp != new_spp:
                spp_transitions[(old_spp, new_spp)] += 1

            old_sp = float(ch.get("old_seller_price", 0))
            new_sp = float(ch.get("new_seller_price", 0))
            if ch.get("seller_price_changed") and round(old_sp) != round(new_sp):
                if new_sp > old_sp:
                    price_up_count += 1
                else:
                    price_down_items.append(ch)

            if ch.get("below_stop"):
                below_stop_items.append(ch)

        lines = [
            f"{mp_emoji} **{mp_short} | Магазин «{safe_shop}»**",
            f"⚡️ **Отчёт об изменении цен и скидок**\n"
        ]

        # 1. Тревожный блок при пробитии Стоп-цены
        if below_stop_items:
            lines.append(f"🚨 **ВНИМАНИЕ! Цена ниже вашей Стоп-цены: {_plural_goods(len(below_stop_items))}!**")
            for b_item in below_stop_items[:5]:
                b_sku = str(b_item.get("sku", "")).replace("OZON:", "").strip()
                b_title = _clean_md(b_item.get("title") or b_sku)[:20]
                b_sp = round(float(b_item.get("new_seller_price", 0)))
                b_min = round(float(b_item.get("min_price", 0)))
                lines.append(f"• 🛑 `{b_sku}` ({b_title}): **{b_sp} ₽** _(Стоп-цена: {b_min} ₽)_")
            if len(below_stop_items) > 5:
                lines.append(f"• _...и ещё {len(below_stop_items) - 5} SKU (см. в CSV)_")
            lines.append("")

        # 2. Детальный блок по товарам, вошедшим в автоакцию / снизившимся в цене (для WB и Ozon)
        if price_down_items:
            lines.append(
                f"🔥 **АВТОАКЦИЯ / СНИЖЕНИЕ ЦЕНЫ: {_plural_goods(len(price_down_items))} вошли со скидкой!**"
            )
            if active_promo_title:
                lines.append(f"📌 _Активная акция {mp_short}: «{_clean_md(active_promo_title)}»_")
            for d_item in price_down_items[:8]:
                d_sku = str(d_item.get("sku", "")).replace("OZON:", "").strip()
                d_title = _clean_md(d_item.get("title") or d_sku)[:18]
                old_p = round(float(d_item.get("old_seller_price", 0)))
                new_p = round(float(d_item.get("new_seller_price", 0)))
                drop_pct = round(((old_p - new_p) / old_p) * 100, 1) if old_p > 0 else 0
                lines.append(f"• `{d_sku}` ({d_title}): **{old_p} ₽ → {new_p} ₽** (`-{drop_pct}%`)")
            if len(price_down_items) > 8:
                lines.append(f"• _...и ещё {len(price_down_items) - 8} SKU (полный список в CSV ниже)_")
            lines.append("")

        # 3. Изменения СПП / Соинвеста
        for (old_spp, new_spp), count in spp_transitions.most_common():
            chart_icon = "📈" if new_spp > old_spp else "📉"
            lines.append(f"🟡 {chart_icon} {_plural_goods(count)}: {discount_label} {old_spp}% → {new_spp}%")

        if price_up_count > 0:
            lines.append(f"👨‍💻 📈 {_plural_goods(price_up_count)} выросли в цене")

        lines.append(f"\n🟡 — _изменение {discount_label}_")
        lines.append("🔥 — _вход в автоакцию / снижение цены продавца_")

        summary_text = "\n".join(lines)

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, delimiter=";")

        writer.writerow([
            "Магазин",
            "Маркетплейс",
            "Артикул (SKU)",
            "Артикул продавца / Название",
            "Цена продавца (Было, ₽)",
            "Цена продавца (Стало, ₽)",
            "Скидка продавца / Автоакция (%)",
            "Ваша Стоп-цена (₽)",
            "Цена клиента без кошелька (Стало, ₽)",
            wallet_col_name,
            f"{discount_label} Было (%)",
            f"{discount_label} Стало (%)",
            "Статус"
        ])

        for ch in changes:
            raw_sku = str(ch.get("sku", "")).replace("OZON:", "").replace("WB:", "").strip()
            title = csv_safe(ch.get("title") or raw_sku)
            old_sp = round(float(ch.get("old_seller_price", 0)))
            new_sp = round(float(ch.get("new_seller_price", 0)))
            min_p = round(float(ch.get("min_price") or 0))
            client_p = round(float(ch.get("new_client_price", 0)))
            wallet_p = round(float(ch.get("new_wallet_price", client_p)))
            old_spp = int(ch.get("old_spp", 0))
            new_spp = int(ch.get("new_spp", 0))

            diff_pct_str = "0%"
            if old_sp > 0 and new_sp != old_sp:
                pct = round(((new_sp - old_sp) / old_sp) * 100, 1)
                diff_pct_str = f"{pct:+.1f}%".replace(".", ",")

            status_parts = []
            if ch.get("below_stop"):
                status_parts.append(f"🛑 НИЖЕ СТОП-ЦЕНЫ ({min_p} ₽)!")
            if ch.get("seller_price_changed") and new_sp != old_sp:
                if new_sp < old_sp:
                    promo_tag = f" ({active_promo_title})" if active_promo_title else ""
                    status_parts.append(f"🔥 Автоакция / Снижение цены{promo_tag}: {old_sp}→{new_sp} ₽ ({diff_pct_str})")
                else:
                    status_parts.append(f"📈 Повышение цены: {old_sp}→{new_sp} ₽ ({diff_pct_str})")
            if ch.get("spp_changed") and new_spp != old_spp:
                status_parts.append(f"{discount_label}: {old_spp}%→{new_spp}%")

            writer.writerow([
                csv_safe(shop_name),
                mp_label,
                f'="{raw_sku}"',
                title,
                old_sp,
                new_sp,
                diff_pct_str,
                min_p if min_p > 0 else "—",
                client_p,
                wallet_p,
                f"{old_spp}%",
                f"{new_spp}%",
                " | ".join(status_parts) if status_parts else "Изменение"
            ])

        csv_bytes = output.getvalue().encode("utf-8")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        safe_slug = "".join(c if c.isalnum() else "_" for c in (shop_name or "shop"))[:20]
        filename = f"changes_{marketplace.lower()}_{safe_slug}_{timestamp}.csv"
        document = BufferedInputFile(csv_bytes, filename=filename)

        sent_msg = await self.bot.send_message(chat_id=chat_id, text=summary_text, parse_mode="Markdown")
        await self.bot.send_document(
            chat_id=chat_id,
            document=document,
            caption=f"📁 Детальный отчёт по изменениям: **{safe_shop} ({mp_label})**",
            parse_mode="Markdown",
            reply_to_message_id=sent_msg.message_id
        )

    async def send_promotion_alert(
        self,
        chat_id: int,
        promo: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        seller_id: int,
        marketplace: str = "WB",
        shop_name: str = "Мой магазин"
    ):
        mp_emoji = "🟣" if marketplace.lower() == "wb" else "🔵"
        mp_label = "WB" if marketplace.lower() == "wb" else "Ozon"
        mp_full = "Wildberries" if marketplace.lower() == "wb" else "Ozon"
        safe_shop = _clean_md(shop_name)

        promo_id = promo.get("id") or promo.get("action_id") or 0
        name = _clean_md(promo.get("name") or promo.get("title") or "Новая акция")
        start = _format_date(promo.get("startDateTime") or promo.get("date_start") or "")
        end = _format_date(promo.get("endDateTime") or promo.get("date_end") or "")
        promo_type = str(promo.get("type") or "regular").lower()
        is_auto = (promo_type == "auto")
        type_str = "⚡️ Автоакция WB (автовход со стороны маркетплейса)" if is_auto else "🎯 Обычная акция (добровольный вход по артикулам)"

        # Сценарий 1: Это Автоакция WB, для которой API WB закрывает поштучную выгрузку артикулов
        if is_auto and not candidates:
            in_promo_cnt = int(promo.get("inPromoActionTotal") or 0)
            not_in_promo_cnt = int(promo.get("notInPromoActionTotal") or 0)
            total_auto_cnt = in_promo_cnt + not_in_promo_cnt
            desc_short = _clean_md(promo.get("description") or "")[:300]

            lines = [
                f"{mp_emoji} **{mp_label} | Магазин «{safe_shop}»**",
                f"⚡️ **Сводка по автоакции Wildberries**\n",
                f"📌 **{name}**",
                f"📅 Период: `{start}` — `{end}`",
                f"⚙️ Формат: _{type_str}_\n",
            ]

            if total_auto_cnt > 0:
                lines.extend([
                    f"📊 **Статус товаров вашего магазина в этой автоакции:**",
                    f"• ✅ Уже участвуют в автоакции: **{_plural_goods(in_promo_cnt)}**",
                    f"• ➖ Не участвуют (вне акции): **{_plural_goods(not_in_promo_cnt)}**\n",
                ])

            if desc_short:
                lines.append(f"📝 **Условия WB:** _{desc_short}..._\n")

            lines.extend([
                "ℹ️ **Почему нет CSV-таблицы и кнопок входа?**",
                "Для акций с типом **«Автоакция» (`⚡️`)** сервер Wildberries не передаёт поштучный список цен через открытый API.",
                "👉 Проверить список товаров или исключить невыгодные позиции из автоакции можно в личном кабинете: **WB Партнёры → Цены и скидки → Календарь акций**."
            ])

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад к списку акций", callback_data="back_to_promos_list")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")]
            ])
            await self.bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines),
                parse_mode="Markdown",
                reply_markup=kb
            )
            return

        # Сценарий 2: Обычная акция с поштучным расчётом рентабельности
        profitable_count = 0
        moderate_count = 0
        unprofitable_count = 0
        below_stop_count = 0
        already_in_count = 0
        already_in_below_stop = 0
        new_drops = []

        csv_rows = []
        for item in candidates:
            sku = str(item.get("sku", "")).replace("OZON:", "").strip()
            title = csv_safe(item.get("title") or sku)
            curr_price = float(item.get("current_price", 0))
            plan_price = float(item.get("plan_price", 0))
            min_price = float(item.get("min_price") or 0.0)
            in_action = bool(item.get("in_action", False))

            if curr_price > 0 and plan_price > 0:
                diff_rub = max(0, round(curr_price - plan_price))
                drop_pct = max(0.0, round(((curr_price - plan_price) / curr_price) * 100, 1))
            else:
                diff_rub = 0
                drop_pct = 0.0

            if in_action:
                already_in_count += 1
                if min_price > 0 and plan_price < min_price:
                    already_in_below_stop += 1
                    verdict = f"⚠️ Уже в акции, но НИЖЕ стоп-цены ({round(min_price)} ₽)!"
                else:
                    verdict = "✅ Уже участвует в акции"
            else:
                new_drops.append(drop_pct)
                if min_price > 0 and plan_price < min_price:
                    below_stop_count += 1
                    unprofitable_count += 1
                    verdict = f"🛑 Заблокирован: ниже стоп-цены ({round(min_price)} ₽)"
                elif drop_pct <= 5.0:
                    profitable_count += 1
                    verdict = "🟢 Выгодно (скидка ≤ 5%)"
                elif drop_pct <= 8.0:
                    moderate_count += 1
                    verdict = "🟡 Умеренно (скидка 5–8%)"
                else:
                    unprofitable_count += 1
                    verdict = "🔴 Сильный демпинг (> 8%)"

            csv_rows.append([
                csv_safe(shop_name),
                mp_full,
                f'="{sku}"',
                title,
                "Уже в акции" if in_action else "Кандидат (не в акции)",
                round(curr_price),
                round(plan_price),
                round(min_price) if min_price > 0 else "—",
                f"-{diff_rub} ₽" if diff_rub > 0 else "0 ₽",
                f"-{drop_pct}%" if drop_pct > 0 else "0%",
                verdict
            ])

        total_candidates = len(candidates)
        new_candidates_count = total_candidates - already_in_count
        avg_drop = round(sum(new_drops) / len(new_drops), 1) if new_drops else 0.0
        good_and_mod = profitable_count + moderate_count
        safe_all_new = new_candidates_count - below_stop_count

        if total_candidates == 0:
            overall_advice = "ℹ️ **Подходящих товаров пока нет** (список артикулов для этой акции пуст)."
        elif new_candidates_count == 0:
            overall_advice = f"✅ **Все подходящие товары ({already_in_count} SKU) уже участвуют в этой акции!**"
        elif profitable_count > 0 and avg_drop <= 5.0 and below_stop_count == 0:
            overall_advice = (
                f"✅ **Рекомендация: ВСТУПАТЬ.**\n"
                f"Мягкие условия: **{profitable_count} новых SKU** проходят со скидкой ≤ 5%."
            )
        elif good_and_mod > 0:
            overall_advice = (
                f"⚖️ **Рекомендация: ВСТУПАТЬ ВЫБОРОЧНО.**\n"
                f"Доступно для выгодного входа (≤ 5%): **{profitable_count} SKU**, умеренно (5–8%): **{moderate_count} SKU**.\n"
                f"Остальные **{unprofitable_count} SKU** требуют глубокой скидки или ниже вашей Стоп-цены."
            )
        else:
            overall_advice = (
                f"⛔️ **Рекомендация: НЕ ВСТУПАТЬ.**\n"
                f"По оставшимся вне акции товарам ({new_candidates_count} SKU) требуется сильный демпинг (в среднем `-{avg_drop}%`) либо цена ниже вашей Стоп-цены."
            )

        lines = [
            f"{mp_emoji} **{mp_label} | Магазин «{safe_shop}»**",
            f"🔥 **Акция и анализ рентабельности**\n",
            f"📌 **{name}**",
            f"📅 Период: `{start}` — `{end}`",
            f"⚙️ Формат: _{type_str}_\n",
            f"📦 **Всего подходит под акцию: {_plural_goods(total_candidates)}**",
        ]

        if already_in_count > 0:
            lines.append(f"• ✅ Уже участвуют в акции: **{_plural_goods(already_in_count)}**")
            if already_in_below_stop > 0:
                lines.append(f"  _(⚠️ из них {already_in_below_stop} SKU сейчас в акции с ценой ниже вашей Стоп-цены!)_")

        if new_candidates_count > 0:
            lines.extend([
                f"\n🆕 **Кандидаты для добавления ({_plural_goods(new_candidates_count)} вне акции):**",
                f"• 🟢 Выгодно (скидка ≤ 5%): **{_plural_goods(profitable_count)}**",
                f"• 🟡 Умеренно (скидка 5–8%): **{_plural_goods(moderate_count)}**",
                f"• 🔴 Сильный демпинг (> 8%): **{_plural_goods(unprofitable_count - below_stop_count)}**",
            ])
            if below_stop_count > 0:
                lines.append(f"• 🛑 Защищены вашей Стоп-ценой: **{_plural_goods(below_stop_count)}** _(не будут добавлены)_")

            lines.append(f"📉 Средняя требуемая скидка по новым товарам: **-{avg_drop}%**\n")
        else:
            lines.append("")

        lines.append(overall_advice)

        buttons = []
        mp_code = marketplace.lower()

        if profitable_count > 0:
            buttons.append([InlineKeyboardButton(
                text=f"🟢 Добавить выгодные ≤ 5% ({profitable_count} SKU)",
                callback_data=f"promo_join_prof_{mp_code}_{seller_id}_{promo_id}"
            )])
        if moderate_count > 0:
            buttons.append([InlineKeyboardButton(
                text=f"🟡 Добавить выгодные + умеренные ≤ 8% ({good_and_mod} SKU)",
                callback_data=f"promo_join_mod_{mp_code}_{seller_id}_{promo_id}"
            )])
        if safe_all_new > 0 and safe_all_new > good_and_mod:
            buttons.append([InlineKeyboardButton(
                text=f"🚀 Добавить все разрешённые Стоп-ценой ({safe_all_new} SKU)",
                callback_data=f"promo_join_all_{mp_code}_{seller_id}_{promo_id}"
            )])

        if new_candidates_count > 0:
            buttons.append([InlineKeyboardButton(
                text="🚫 Не добавлять товары в акцию",
                callback_data=f"promo_decline_{mp_code}_{seller_id}_{promo_id}"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text="🏠 В главное меню",
                callback_data="back_to_main_menu"
            )])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        sent_msg = await self.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="Markdown",
            reply_markup=kb
        )

        if csv_rows:
            output = io.StringIO()
            output.write("\ufeff")
            writer = csv.writer(output, delimiter=";")
            writer.writerow([
                "Магазин",
                "Маркетплейс",
                "Артикул (SKU)",
                "Артикул продавца / Название",
                "Статус участия",
                "Текущая цена продавца (₽)",
                "Целевая цена для акции (₽)",
                "Ваша Стоп-цена (₽)",
                "Потеря в цене (₽)",
                "Требуемая скидка (%)",
                "Вердикт бота"
            ])
            writer.writerows(csv_rows)

            csv_bytes = output.getvalue().encode("utf-8")
            safe_slug = "".join(c if c.isalnum() else "_" for c in (shop_name or "shop"))[:20]
            filename = f"promo_{mp_code}_{safe_slug}_{promo_id}.csv"
            document = BufferedInputFile(csv_bytes, filename=filename)

            await self.bot.send_document(
                chat_id=chat_id,
                document=document,
                caption=f"📋 Детальный расчёт по товарам: **{safe_shop} ({mp_full})** — акция «{name}»",
                parse_mode="Markdown",
                reply_to_message_id=sent_msg.message_id
            )