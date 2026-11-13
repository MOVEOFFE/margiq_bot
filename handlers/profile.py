from datetime import datetime
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func

from database import AsyncSessionFactory
from models import UserAccount, Seller
from config import settings
from handlers.base import ensure_user_account, clean_md, MENU_BUTTONS

router = Router()


class ProfileStates(StatesGroup):
    waiting_for_promo = State()


def _profile_keyboard() -> InlineKeyboardMarkup:
    """Удобное объединенное меню кабинета: тарифы, промокод и справка."""
    kb = []
    for key, t in settings.tariffs.items():
        kb.append([InlineKeyboardButton(
            text=f"💎 Продлить: {t['name']} ({t['price']} ₽ / {t['days']} дн.)",
            callback_data=f"buy_{key}"
        )])
    kb.append([InlineKeyboardButton(text=f"➕ Купить доп. магазин ({settings.EXTRA_SHOP_PRICE_RUB} ₽)", callback_data="buy_extra_shop")])
    kb.append([InlineKeyboardButton(text="🏷 Ввести промокод", callback_data="profile_promo")])
    kb.append([InlineKeyboardButton(text="❓ Помощь и FAQ", callback_data="goto_faq")])
    kb.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def _profile_data(tg_id: int, username: str | None, full_name: str | None):
    acc = await ensure_user_account(tg_id, username, full_name)
    async with AsyncSessionFactory() as session:
        active_shops = (await session.execute(
            select(func.count(Seller.id)).where(
                Seller.telegram_id == tg_id,
                (Seller.wb_token_encrypted.is_not(None)) | (Seller.ozon_api_key_encrypted.is_not(None))
            )
        )).scalar() or 0
    return acc, active_shops


def _profile_text(acc, active_shops: int, from_user) -> str:
    now = datetime.utcnow()
    name = clean_md(from_user.full_name or acc.full_name or "—")
    username = f"@{from_user.username}" if from_user.username else "—"
    tariff = acc.tariff or "trial"

    if acc.is_banned:
        status = "🚫 заблокирован"
        until = "—"
    elif acc.subscription_until and acc.subscription_until > now:
        days_left = (acc.subscription_until - now).days
        status = "✅ активна"
        until = f"{acc.subscription_until.strftime('%d.%m.%Y')} ({days_left} дн.)"
    else:
        status = "❌ истекла"
        until = "—"

    promo = acc.saved_promo or "не введён"

    return (
        "👤 **Личный кабинет и Тариф**\n\n"
        f"🙍 Имя: **{name}**\n"
        f"🔗 Telegram: `{username}`\n"
        f"🆔 ID: `{from_user.id}`\n\n"
        f"💎 Текущий тариф: **{tariff}** — {status}\n"
        f"📅 Подписка действует до: **{until}**\n"
        f"🏪 Подключено магазинов: **{active_shops} из {acc.max_shops}**\n"
        f"🏷 Сохраненный промокод: **{promo}**\n\n"
        "👇 **Выберите тариф для продления или управления:**"
    )


@router.message(Command("profile"))
@router.message(F.text.in_(["👤 Профиль", "👤 Мой профиль", "💎 Тарифы", "💎 Тарифы и оплата", "👤 Профиль и Тариф"]))
async def cmd_profile(message: Message, state: FSMContext):
    await state.clear()
    acc, active_shops = await _profile_data(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer(_profile_text(acc, active_shops, message.from_user), parse_mode="Markdown", reply_markup=_profile_keyboard())


@router.callback_query(F.data.in_({"goto_profile", "goto_tariffs"}))
async def cb_goto_profile(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    acc, active_shops = await _profile_data(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    try:
        await callback.message.edit_text(_profile_text(acc, active_shops, callback.from_user), parse_mode="Markdown", reply_markup=_profile_keyboard())
    except Exception:
        await callback.message.answer(_profile_text(acc, active_shops, callback.from_user), parse_mode="Markdown", reply_markup=_profile_keyboard())
    await callback.answer()


@router.callback_query(F.data == "profile_promo")
async def cb_profile_promo(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="goto_profile")]])
    await callback.message.answer("🏷 Отправьте промокод текстом (например `WELCOME20`):", parse_mode="Markdown", reply_markup=kb)
    await state.set_state(ProfileStates.waiting_for_promo)
    await callback.answer()


@router.message(ProfileStates.waiting_for_promo)
async def process_profile_promo(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("⚠️ Отправьте промокод текстом.")
        return

    if message.text.strip() in MENU_BUTTONS or message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Ввод промокода отменён. Выберите нужный раздел в меню ещё раз.")
        return

    code = message.text.strip().upper()
    from handlers.subscription import _validate_promo
    promo = await _validate_promo(code, tg_id=message.from_user.id)
    if not promo:
        await message.answer("❌ Промокод не найден, истёк, исчерпан или уже был использован вами ранее.")
        return

    async with AsyncSessionFactory() as session:
        acc = (await session.execute(select(UserAccount).where(UserAccount.telegram_id == message.from_user.id))).scalars().first()
        if not acc:
            acc = UserAccount(telegram_id=message.from_user.id)
            session.add(acc)
        acc.saved_promo = promo.code
        await session.commit()

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 Вернуться в кабинет", callback_data="goto_profile")]])
    await message.answer(
        f"✅ Промокод `{promo.code}` сохранён — скидка **{promo.discount_percent}%**.\n"
        f"Он применится автоматически при следующей оплате в разделе 👤 Профиль и Тариф.",
        parse_mode="Markdown",
        reply_markup=kb
    )


# --- FAQ / Помощь ---

def _faq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 В личный кабинет", callback_data="goto_profile")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")],
    ])


def _faq_text() -> str:
    return (
        "❓ **Ответы на частые вопросы (FAQ)**\n\n"
        "🏪 **Как подключить магазин?**\n"
        "«🏪 Мои магазины» → «➕ Добавить новый магазин» → выберите WB или Ozon и вставьте токен.\n\n"
        "🔑 **Где взять токен WB?**\n"
        "Личный кабинет WB → Настройки → Доступ к API. Нужны права «Цены и скидки» и «Статистика».\n\n"
        "🔑 **Где взять ключ Ozon?**\n"
        "Личный кабинет Ozon → Настройки → API-ключи. Отправьте боту `CLIENT_ID API_KEY` через пробел.\n\n"
        "🔔 **Почему нет уведомлений о СПП?**\n"
        "Бот сравнивает данные с прошлым опросом. Порог задаётся в «⚙️ Настройки».\n\n"
        "🔥 **Как работают акции и Стоп-цены?**\n"
        "В разделе «📊 Мои товары» вы можете задать минимальную рентабельную цену (Стоп-цену) для каждого артикула. "
        "В разделе «🔥 Акции» бот рассчитает скидку и заблокирует добавление товаров, если цена акции ниже вашей Стоп-цены.\n\n"
        "💎 **Тарифы и оплата:**\n"
        f"• Trial — {settings.TRIAL_DAYS} дней бесплатно, {settings.TRIAL_MAX_SHOPS} магазин\n"
        f"• Basic — {settings.BASIC_PRICE_RUB} ₽ / {settings.BASIC_DAYS} дн., {settings.BASIC_MAX_SHOPS} магазина\n"
        f"• Business — {settings.BUSINESS_PRICE_RUB} ₽ / {settings.BUSINESS_DAYS} дн., {settings.BUSINESS_MAX_SHOPS} магазина\n"
        f"• Доп. магазин — {settings.EXTRA_SHOP_PRICE_RUB} ₽\n\n"
        "🏷 **Где ввести промокод?**\n"
        "В разделе «👤 Профиль и Тариф» нажмите «🏷 Ввести промокод».\n\n"
        "ℹ️ Если возникли вопросы — напишите администратору."
    )


@router.callback_query(F.data == "goto_faq")
async def cb_goto_faq(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text(_faq_text(), parse_mode="Markdown", reply_markup=_faq_keyboard())
    except Exception:
        pass
    await callback.answer()


@router.message(Command("help"))
@router.message(F.text.in_(["❓ Помощь", "❓ Ответы на вопросы", "❓ FAQ"]))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(_faq_text(), parse_mode="Markdown", reply_markup=_faq_keyboard())