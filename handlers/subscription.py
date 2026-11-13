import uuid
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from database import AsyncSessionFactory
from models import UserAccount, Payment, PromoCode
from config import settings
from services.payments import YooKassa
from handlers.base import MENU_BUTTONS

router = Router()


class PromoStates(StatesGroup):
    waiting_for_promo = State()


def _clean_md(text: str | None) -> str:
    if not text:
        return ""
    return str(text).replace("_", " ").replace("*", "").replace("`", "").replace("[", "(").replace("]", ")")


def _plan_text(acc: UserAccount) -> str:
    now = datetime.utcnow()
    if acc.is_banned:
        return "🚫 Аккаунт заблокирован"
    if acc.subscription_until and acc.subscription_until > now:
        days_left = (acc.subscription_until - now).days
        return f"✅ Тариф **{acc.tariff or 'trial'}** до {acc.subscription_until.strftime('%d.%m.%Y')} ({days_left} дн.) · магазинов: {acc.max_shops}"
    return "❌ Подписка не активна"


def _tariffs_keyboard() -> InlineKeyboardMarkup:
    kb = []
    for key, t in settings.tariffs.items():
        kb.append([InlineKeyboardButton(
            text=f"💎 {t['name']} — {t['price']} ₽ ({t['days']} дн., {t['max_shops']} маг.)",
            callback_data=f"buy_{key}"
        )])
    kb.append([InlineKeyboardButton(
        text=f"➕ Добавить магазин — {settings.EXTRA_SHOP_PRICE_RUB} ₽",
        callback_data="buy_extra_shop"
    )])
    kb.append([InlineKeyboardButton(text="🏷 Ввести промокод", callback_data="profile_promo")])
    kb.append([InlineKeyboardButton(text="❓ Помощь и FAQ", callback_data="goto_faq")])
    kb.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def _get_account(tg_id: int) -> UserAccount:
    async with AsyncSessionFactory() as session:
        acc = (await session.execute(select(UserAccount).where(UserAccount.telegram_id == tg_id))).scalars().first()
        if not acc:
            acc = UserAccount(
                telegram_id=tg_id,
                max_shops=settings.TRIAL_MAX_SHOPS,
                subscription_until=datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS),
            )
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
        return acc


async def _validate_promo(code_str: str, tg_id: int | None = None) -> PromoCode | None:
    code = code_str.strip().upper()
    async with AsyncSessionFactory() as session:
        promo = (await session.execute(select(PromoCode).where(PromoCode.code == code))).scalars().first()
        if not promo or not promo.active:
            return None
        if promo.expires_at and promo.expires_at < datetime.utcnow():
            return None
        if promo.used_count >= promo.max_uses:
            return None

        # Защита от повторного использования одного промокода одним и тем же пользователем
        if tg_id:
            already_used = (await session.execute(
                select(Payment.id).where(
                    Payment.telegram_id == tg_id,
                    Payment.promo_code == code,
                    Payment.status == "succeeded"
                ).limit(1)
            )).scalars().first()
            if already_used:
                return None

    return promo


async def grant_purchase(payment_id: int) -> str:
    """Безопасная активация покупки с защитой от двойного списания (Race Condition)."""
    async with AsyncSessionFactory() as session:
        payment = (await session.execute(select(Payment).where(Payment.id == payment_id))).scalars().first()
        if not payment:
            return "Платёж не найден"

        if payment.status == "succeeded":
            return "Платёж уже был успешно активирован ранее."

        acc = (await session.execute(
            select(UserAccount).where(UserAccount.telegram_id == payment.telegram_id)
        )).scalars().first()
        if not acc:
            acc = UserAccount(
                telegram_id=payment.telegram_id,
                max_shops=settings.TRIAL_MAX_SHOPS,
                subscription_until=datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS),
            )
            session.add(acc)

        now = datetime.utcnow()
        if payment.purpose == "extra_shop":
            acc.max_shops = (acc.max_shops or 0) + payment.extra_shops
            acc.is_banned = False
            summary = f"➕ Добавлен магазин. Теперь доступно магазинов: **{acc.max_shops}**."
        else:
            t = settings.tariffs.get(payment.tariff or payment.purpose)
            days = t["days"] if t else 31
            new_base_shops = t["max_shops"] if t else 2
            name = t["name"] if t else (payment.tariff or payment.purpose)

            base = acc.subscription_until if (acc.subscription_until and acc.subscription_until > now) else now
            acc.subscription_until = base + timedelta(days=days)

            acc.max_shops = max(acc.max_shops or 1, new_base_shops)
            acc.tariff = payment.tariff or payment.purpose
            acc.is_banned = False
            summary = f"💎 Тариф **{name}** активирован до {acc.subscription_until.strftime('%d.%m.%Y')} (магазинов: **{acc.max_shops}**)."

        if payment.promo_code:
            promo = (await session.execute(
                select(PromoCode).where(PromoCode.code == payment.promo_code)
            )).scalars().first()
            if promo:
                promo.used_count = (promo.used_count or 0) + 1

        acc.saved_promo = None
        payment.status = "succeeded"
        await session.commit()
        return summary


async def _create_payment(tg_id: int, purpose: str, promo_code: str | None = None):
    """Создаёт платёж. Если промокод дает 100% скидку, оформляет без вызова ЮKassa."""
    if purpose == "extra_shop":
        base_price = settings.EXTRA_SHOP_PRICE_RUB
        desc = "Дополнительный магазин Margiq"
        tariff = None
        extra = 1
    else:
        t = settings.tariffs.get(purpose)
        if not t:
            return False, "Неизвестный тариф", None, None
        base_price = t["price"]
        desc = f"Подписка Margiq «{t['name']}»"
        tariff = purpose
        extra = 0

    if not promo_code:
        async with AsyncSessionFactory() as session:
            acc = (await session.execute(select(UserAccount).where(UserAccount.telegram_id == tg_id))).scalars().first()
            if acc and acc.saved_promo:
                promo_code = acc.saved_promo

    amount = float(base_price)
    promo = None
    if promo_code:
        promo = await _validate_promo(promo_code, tg_id=tg_id)
        if not promo:
            # Сбрасываем неактуальный сохраненный промокод, чтобы не блокировать покупку
            async with AsyncSessionFactory() as session:
                acc = (await session.execute(select(UserAccount).where(UserAccount.telegram_id == tg_id))).scalars().first()
                if acc and acc.saved_promo == promo_code:
                    acc.saved_promo = None
                    await session.commit()
            return False, "Сохранённый промокод истёк, исчерпан или уже был использован вами ранее. Попробуйте оплату ещё раз.", None, None
        amount = round(base_price * (1 - promo.discount_percent / 100))

    if amount <= 0:
        async with AsyncSessionFactory() as session:
            p = Payment(
                telegram_id=tg_id,
                yookassa_id=f"promo_free_{uuid.uuid4().hex[:8]}",
                purpose=purpose,
                amount_rub=0.0,
                status="pending",
                tariff=tariff,
                extra_shops=extra,
                promo_code=promo.code if promo else None,
            )
            session.add(p)
            await session.commit()
            await session.refresh(p)

        summary = await grant_purchase(p.id)
        return True, summary, p.id, None

    yk = YooKassa()
    result = await yk.create_payment(amount, desc, {"telegram_id": tg_id, "purpose": purpose})
    if "error" in result:
        return False, f"Ошибка создания платежа: {result['error']}", None, None

    confirmation_url = (result.get("confirmation") or {}).get("confirmation_url", "")
    yookassa_id = result.get("id", "")
    async with AsyncSessionFactory() as session:
        p = Payment(
            telegram_id=tg_id,
            yookassa_id=yookassa_id,
            purpose=purpose,
            amount_rub=amount,
            status="pending",
            tariff=tariff,
            extra_shops=extra,
            promo_code=promo.code if promo else None,
        )
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return True, "", p.id, confirmation_url


def _tariffs_text(acc: UserAccount) -> str:
    lines = [
        "💎 **Тарифы Margiq**\n",
        f"Ваш текущий тариф:\n{_plan_text(acc)}\n",
        "**Доступные планы:**",
    ]
    for key, t in settings.tariffs.items():
        lines.append(f"• **{t['name']}** — {t['price']} ₽ / {t['days']} дн. / до {t['max_shops']} магазинов")
    lines.append(f"• **➕ Доп. магазин** — {settings.EXTRA_SHOP_PRICE_RUB} ₽ (+1 магазин до конца подписки)")

    if acc.saved_promo:
        lines.append(f"\n🏷 Активный промокод для скидки: `{acc.saved_promo}`")

    if not YooKassa().configured:
        lines.append("\n⚠️ _Оплата через ЮKassa в тестовом режиме — обратитесь к администратору._")
    return "\n".join(lines)


@router.message(Command("tariff"))
async def cmd_tariffs(message: Message, state: FSMContext):
    await state.clear()
    acc = await _get_account(message.from_user.id)
    await message.answer(_tariffs_text(acc), parse_mode="Markdown", reply_markup=_tariffs_keyboard())


@router.callback_query(F.data.startswith("buy_"))
async def cb_buy(callback: CallbackQuery, state: FSMContext):
    purpose = callback.data[4:]
    if purpose == "extra_shop":
        acc = await _get_account(callback.from_user.id)
        if acc.is_banned or not acc.subscription_until or acc.subscription_until < datetime.utcnow():
            await callback.answer("Нужна активная подписка", show_alert=True)
            await callback.message.answer("⚠️ Для доплаты за магазин сначала нужна активная подписка — купите тариф.")
            return

    ok, err_or_summary, payment_id, url = await _create_payment(callback.from_user.id, purpose)
    if not ok:
        await callback.answer("Ошибка", show_alert=True)
        await callback.message.answer(f"⚠️ {err_or_summary}")
        return

    if not url:
        await callback.message.edit_text(f"🎉 **Тариф активирован по промокоду!**\n{err_or_summary}", parse_mode="Markdown")
        await callback.answer("Активировано!")
        return

    await _send_payment_link(callback, payment_id, url)
    await callback.answer()


async def _send_payment_link(event, payment_id: int, url: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Оплатить в ЮKassa", url=url)],
        [InlineKeyboardButton(text="✅ Я оплатил, проверить", callback_data=f"check_pay_{payment_id}")],
        [InlineKeyboardButton(text="🏷 У меня есть промокод", callback_data=f"apply_promo_{payment_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_pay_{payment_id}")],
    ])
    text = (
        "💳 **Оплата подписки**\n\n"
        "1. Нажмите «🔗 Оплатить в ЮKassa» и завершите платеж.\n"
        "2. Вернитесь сюда и нажмите «✅ Я оплатил, проверить»."
    )
    if isinstance(event, Message):
        await event.answer(text, parse_mode="Markdown", reply_markup=kb)
    else:
        try:
            await event.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            pass


@router.callback_query(F.data.startswith("apply_promo_"))
async def cb_apply_promo_btn(callback: CallbackQuery, state: FSMContext):
    payment_id = int(callback.data.split("_")[-1])
    async with AsyncSessionFactory() as session:
        p = (await session.execute(select(Payment).where(Payment.id == payment_id))).scalars().first()
        if p and p.status == "pending":
            p.status = "canceled"
            await session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="goto_profile")]])
    await state.set_state(PromoStates.waiting_for_promo)
    await callback.message.answer("🏷 Отправьте промокод текстом (например `WELCOME20`):", parse_mode="Markdown", reply_markup=kb)
    await callback.answer()


@router.message(PromoStates.waiting_for_promo)
async def process_promo_code_direct(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("⚠️ Отправьте промокод текстом.")
        return

    if message.text.strip() in MENU_BUTTONS or message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Ввод промокода отменён. Выберите нужный раздел в меню ещё раз.")
        return

    code = message.text.strip()
    promo = await _validate_promo(code, tg_id=message.from_user.id)
    if not promo:
        await message.answer("❌ Промокод не найден, истёк, исчерпан или уже был использован вами. Попробуйте другой код.")
        return

    async with AsyncSessionFactory() as session:
        acc = (await session.execute(select(UserAccount).where(UserAccount.telegram_id == message.from_user.id))).scalars().first()
        if not acc:
            acc = UserAccount(telegram_id=message.from_user.id)
            session.add(acc)
        acc.saved_promo = promo.code
        await session.commit()

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Перейти к тарифам", callback_data="goto_profile")]])
    await message.answer(
        f"✅ Промокод `{promo.code}` сохранён (скидка **{promo.discount_percent}%**).\n"
        f"Выберите нужный тариф — скидка применится автоматически!",
        parse_mode="Markdown",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("check_pay_"))
async def cb_check_pay(callback: CallbackQuery):
    payment_id = int(callback.data.split("_")[-1])
    async with AsyncSessionFactory() as session:
        p = (await session.execute(
            select(Payment).where(Payment.id == payment_id, Payment.telegram_id == callback.from_user.id)
        )).scalars().first()

    if not p:
        await callback.answer("Платёж не найден", show_alert=True)
        return

    if p.status == "succeeded":
        await callback.answer("Уже активировано")
        return

    yk = YooKassa()
    data = await yk.get_payment(p.yookassa_id)
    status = data.get("status")
    if status == "succeeded":
        summary = await grant_purchase(payment_id)
        await callback.message.edit_text(f"🎉 **Оплата прошла успешно!**\n{summary}", parse_mode="Markdown")
        await callback.answer("Оплата подтверждена")
    elif status == "canceled":
        async with AsyncSessionFactory() as session:
            p2 = (await session.execute(select(Payment).where(Payment.id == payment_id))).scalars().first()
            if p2:
                p2.status = "canceled"
                await session.commit()
        await callback.message.edit_text("🚫 Платёж отменён.")
        await callback.answer("Платёж отменён")
    else:
        await callback.answer("⏳ Оплата ещё не поступила. Попробуйте через минуту.", show_alert=True)


@router.callback_query(F.data.startswith("cancel_pay_"))
async def cb_cancel_pay(callback: CallbackQuery):
    payment_id = int(callback.data.split("_")[-1])
    async with AsyncSessionFactory() as session:
        p = (await session.execute(
            select(Payment).where(Payment.id == payment_id, Payment.telegram_id == callback.from_user.id)
        )).scalars().first()
        if p and p.status == "pending":
            p.status = "canceled"
            await session.commit()
    try:
        await callback.message.edit_text("🚫 Оплата отменена.")
    except Exception:
        pass
    await callback.answer("Отменено")