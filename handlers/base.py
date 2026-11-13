import io
import csv
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func, update, delete
from sqlalchemy.orm import selectinload

from database import AsyncSessionFactory
from models import UserAccount, Seller, SellerSettings, Product, init_db
from security import encrypt_token, decrypt_token
from services.wb_client import WBClient
from services.ozon_client import OzonClient
from tasks.scheduler import check_prices
from utils import csv_safe

from config import settings
from handlers.admin import is_user_admin

router = Router()

# Список кнопок главного меню для защиты от случайного ввода в FSM и перехвата текстовым поиском
MENU_BUTTONS = {
    "📊 Мои товары", "🔥 Акции", "🔥 Календарь акций", "🎯 Флагманы",
    "🏪 Мои магазины", "👤 Профиль и Тариф", "👤 Профиль", "👤 Мой профиль",
    "💎 Тарифы", "💎 Тарифы и оплата", "⚙️ Настройки",
    "👑 Админ-панель", "🏠 Меню", "🏠 Главное меню",
    "❓ Помощь", "❓ Ответы на вопросы", "❓ FAQ"
}


class AddShopStates(StatesGroup):
    waiting_for_marketplace = State()
    waiting_for_shop_name = State()
    waiting_for_wb_token = State()
    waiting_for_ozon_creds = State()
    waiting_for_min_prices_csv = State()


def clean_md(text: str | None) -> str:
    if not text:
        return ""
    return str(text).replace("_", " ").replace("*", "").replace("`", "").replace("[", "(").replace("]", ")")


def get_cancel_kb(allow_back_to_mp: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура отмены и возврата на шаг выбора маркетплейса."""
    rows = []
    if allow_back_to_mp:
        rows.append([InlineKeyboardButton(text="🔙 Назад к выбору МП", callback_data="back_to_mp_select")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_shop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_main_keyboard(user_id: int | None = None) -> ReplyKeyboardMarkup:
    """Компактное меню селлера (3 ряда, 6 кнопок) для удобства на смартфонах."""
    keyboard_layout = [
        [KeyboardButton(text="📊 Мои товары"), KeyboardButton(text="🔥 Акции")],
        [KeyboardButton(text="🎯 Флагманы"), KeyboardButton(text="🏪 Мои магазины")],
        [KeyboardButton(text="👤 Профиль и Тариф"), KeyboardButton(text="⚙️ Настройки")]
    ]

    if user_id and is_user_admin(user_id):
        keyboard_layout.append([KeyboardButton(text="👑 Админ-панель")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard_layout,
        resize_keyboard=True,
        persistent=True,
        input_field_placeholder="Выберите раздел или отправьте артикул (SKU) 👇"
    )


async def ensure_user_account(user_id: int, username: str | None = None, full_name: str | None = None) -> UserAccount:
    try:
        async with AsyncSessionFactory() as session:
            acc = (await session.execute(
                select(UserAccount).where(UserAccount.telegram_id == user_id)
            )).scalars().first()
    except Exception:
        await init_db()
        async with AsyncSessionFactory() as session:
            acc = (await session.execute(
                select(UserAccount).where(UserAccount.telegram_id == user_id)
            )).scalars().first()

    async with AsyncSessionFactory() as session:
        acc = (await session.execute(
            select(UserAccount).where(UserAccount.telegram_id == user_id)
        )).scalars().first()

        if not acc:
            acc = UserAccount(
                telegram_id=user_id,
                username=username,
                full_name=full_name,
                max_shops=settings.TRIAL_MAX_SHOPS,
                subscription_until=datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS)
            )
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
        else:
            updated = False
            if username and acc.username != username:
                acc.username = username
                updated = True
            if full_name and acc.full_name != full_name:
                acc.full_name = full_name
                updated = True
            if updated:
                await session.commit()
        return acc


async def render_dashboard_text(user_id: int, username: str | None = None, full_name: str | None = None) -> str:
    acc = await ensure_user_account(user_id, username, full_name)

    async with AsyncSessionFactory() as session:
        first_settings = (await session.execute(
            select(SellerSettings).join(Seller).where(Seller.telegram_id == user_id).limit(1)
        )).scalars().first()

        spp_thr = first_settings.spp_delta_threshold if first_settings else 1
        price_thr = int(first_settings.price_delta_threshold) if first_settings else 1

        active_shops_count = (await session.execute(
            select(func.count(Seller.id)).where(
                Seller.telegram_id == user_id,
                (Seller.wb_token_encrypted.is_not(None)) | (Seller.ozon_api_key_encrypted.is_not(None))
            )
        )).scalar() or 0

        total_sku = (await session.execute(
            select(func.count(Product.id)).join(Seller).where(Seller.telegram_id == user_id)
        )).scalar() or 0

        total_flagships = (await session.execute(
            select(func.count(Product.id)).join(Seller).where(
                Seller.telegram_id == user_id,
                Product.is_flagship == True
            )
        )).scalar() or 0

    now = datetime.utcnow()
    if acc.is_banned:
        sub_line = "🚫 **Аккаунт заблокирован**"
    elif acc.subscription_until and acc.subscription_until > now:
        days_left = (acc.subscription_until - now).days
        until_str = acc.subscription_until.strftime("%d.%m.%Y")
        limit_str = "Безлимит" if acc.max_shops >= 100 else f"{active_shops_count} из {acc.max_shops}"
        sub_line = f"✅ **Активна до {until_str}** (`{days_left} дн.`) | Магазины: **{limit_str}**"
    else:
        sub_line = "❌ **Подписка истекла** (перейдите в Профиль для продления)"

    return (
        "🏠 **Главное меню | Margiq Контроль**\n\n"
        f"💎 **Тариф:** {sub_line}\n"
        f"🆔 **Ваш ID:** `{user_id}`\n\n"
        "📋 **Сводка вашего аккаунта:**\n"
        f"• 🏪 Подключено магазинов: **{active_shops_count}**\n"
        f"• 📦 Товаров под мониторингом: **{total_sku} SKU**\n"
        f"• 🎯 Флагманов (опрос каждые 15 мин): **{total_flagships} SKU**\n"
        f"• 🔔 Чувствительность: СПП от **{spp_thr}%** | Цена от **{price_thr} ₽**\n\n"
        "🔎 _Для быстрого поиска товара просто отправьте его артикул (SKU) в чат._"
    )


@router.message(CommandStart())
@router.message(Command("menu"))
@router.message(F.text.in_(["🏠 Меню", "🏠 Главное меню"]))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = await render_dashboard_text(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))


@router.callback_query(F.data == "back_to_main_menu")
async def cb_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = await render_dashboard_text(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    try:
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard(callback.from_user.id))
    except Exception:
        pass
    await callback.answer()


# --- Управление магазинами ---

@router.message(Command("shops"))
@router.message(F.text == "🏪 Мои магазины")
async def cmd_shops(message: Message, state: FSMContext):
    await state.clear()
    acc = await ensure_user_account(message.from_user.id, message.from_user.username, message.from_user.full_name)

    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).where(Seller.telegram_id == message.from_user.id)
        )).scalars().all()

        active_count = sum(1 for s in sellers if (s.wb_token_encrypted or s.ozon_api_key_encrypted))
        limit_str = "Безлимит" if acc.max_shops >= 100 else f"{active_count} из {acc.max_shops}"

        text = f"🏪 **Ваши подключенные магазины** (Лимит тарифа: **{limit_str}**)\n\n"
        buttons = []

        for s in sellers:
            mp_ico = "🟣 WB" if s.marketplace == "wb" else "🔵 Ozon"
            is_configured = bool(s.wb_token_encrypted or s.ozon_api_key_encrypted)
            status = "✅ Активен" if is_configured else "⚠️ Ключ не привязан"
            safe_name = clean_md(s.shop_name)
            text += f"• {mp_ico} — **{safe_name}** ({status})\n"
            buttons.append([
                InlineKeyboardButton(text=f"🔑 Обновить токен: {safe_name}", callback_data=f"update_token_{s.id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_shop_{s.id}")
            ])

        buttons.append([InlineKeyboardButton(text="➕ Добавить новый магазин", callback_data="add_new_shop")])
        buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("update_token_"))
async def cb_update_token(callback: CallbackQuery, state: FSMContext):
    seller_id = int(callback.data.split("_")[-1])
    async with AsyncSessionFactory() as session:
        seller = (await session.execute(
            select(Seller).where(Seller.id == seller_id, Seller.telegram_id == callback.from_user.id)
        )).scalars().first()
    if not seller:
        await callback.answer("Магазин не найден", show_alert=True)
        return

    await state.update_data(update_seller_id=seller.id, marketplace=seller.marketplace, shop_name=seller.shop_name)
    safe_shop = clean_md(seller.shop_name)

    if seller.marketplace == "wb":
        text = (
            f"🔑 Отправьте новый **API-токен Wildberries** для магазина **{safe_shop}**.\n\n"
            f"📌 **Какие параметры выбрать в ЛК WB (Настройки → Доступ к API):**\n"
            f"1. Отметьте категории: **«Цены и скидки»** и **«Статистика»**.\n"
            f"2. ⚠️ Галочку **«Только на чтение» НЕ ставьте** — иначе бот сможет только смотреть цены, но не сможет добавлять ваши товары в акции по кнопке."
        )
        target_state = AddShopStates.waiting_for_wb_token
    else:
        text = (
            f"🔑 Отправьте новые данные **Ozon** для магазина **{safe_shop}** через пробел:\n"
            f"`CLIENT_ID API_KEY`\n\n"
            f"📌 **Где взять ключи в ЛК Ozon Seller:**\n"
            f"1. Перейдите в **Настройки (⚙️) → API интеграции → Seller API**.\n"
            f"2. Скопируйте ваш `Client ID` и нажмите **«Сгенерировать ключ»**.\n"
            f"3. В списке ролей выберите:\n"
            f"   • **«Product read-only»** (или **«Product»**) — для мониторинга товаров и цен;\n"
            f"   • **«Actions»** — для расчёта акций и входа по кнопке _(или **«Actions read-only»**, если нужен только просмотр акций без входа из бота)_\n"
            f"   • Либо просто выберите одну общую роль **«Admin»**.\n"
            f"ℹ️ _Платная подписка Ozon Premium для работы этих методов НЕ требуется._"
        )
        target_state = AddShopStates.waiting_for_ozon_creds

    await callback.message.answer(
        text,
        parse_mode="Markdown",
        reply_markup=get_cancel_kb(allow_back_to_mp=False)
    )
    await state.set_state(target_state)
    await callback.answer()


@router.callback_query(F.data.in_(["add_new_shop", "back_to_mp_select"]))
async def cb_add_shop(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    acc = await ensure_user_account(callback.from_user.id)

    if acc.is_banned or not acc.subscription_until or acc.subscription_until < datetime.utcnow():
        await callback.answer("Ваша подписка истекла!", show_alert=True)
        await callback.message.answer("⚠️ **Ваша подписка не активна.** Перейдите в раздел 👤 Профиль и Тариф для продления.", parse_mode="Markdown")
        return

    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()
        active_shops = sum(1 for s in sellers if (s.wb_token_encrypted or s.ozon_api_key_encrypted))

    if active_shops >= acc.max_shops:
        await callback.answer("Достигнут лимит магазинов по вашему тарифу!", show_alert=True)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Тарифы и доп. магазин", callback_data="goto_tariffs")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")],
        ])
        await callback.message.answer(
            f"⚠️ **Достигнут лимит магазинов ({active_shops} из {acc.max_shops}).**\n"
            f"Купите дополнительный магазин или перейдите на расширенный тариф.",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟣 Wildberries", callback_data="choose_mp_wb"),
            InlineKeyboardButton(text="🔵 Ozon", callback_data="choose_mp_ozon")
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_shop")]
    ])
    try:
        await callback.message.edit_text("Выберите маркетплейс нового магазина:", reply_markup=kb)
    except Exception:
        await callback.message.answer("Выберите маркетплейс нового магазина:", reply_markup=kb)
    await state.set_state(AddShopStates.waiting_for_marketplace)
    await callback.answer()


@router.callback_query(F.data == "cancel_add_shop")
async def cb_cancel_add_shop(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Действие с магазином отменено.")
    except Exception:
        pass
    await callback.answer("Отменено")


@router.callback_query(AddShopStates.waiting_for_marketplace, F.data.startswith("choose_mp_"))
async def cb_choose_mp(callback: CallbackQuery, state: FSMContext):
    mp = callback.data.split("_")[-1]
    mp_title = "Wildberries 🟣" if mp == "wb" else "Ozon 🔵"
    await state.update_data(marketplace=mp)
    await callback.message.edit_text(
        f"Выбран маркетплейс: **{mp_title}**\n\n"
        f"Введите **название магазина**:",
        parse_mode="Markdown",
        reply_markup=get_cancel_kb(allow_back_to_mp=True)
    )
    await state.set_state(AddShopStates.waiting_for_shop_name)
    await callback.answer()


@router.message(AddShopStates.waiting_for_shop_name)
async def process_shop_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(
            "⚠️ Пожалуйста, отправьте название магазина текстом:",
            reply_markup=get_cancel_kb(allow_back_to_mp=True)
        )
        return

    if message.text.strip() in MENU_BUTTONS or message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Добавление магазина отменено. Выберите нужный раздел в меню еще раз.")
        return

    shop_name = clean_md(message.text.strip())[:50]
    await state.update_data(shop_name=shop_name)
    data = await state.get_data()

    if data.get("marketplace") == "wb":
        await message.answer(
            f"🔑 Отправьте **API-токен Wildberries** для магазина *{shop_name}*.\n\n"
            f"📌 **Какие параметры выбрать в ЛК WB (Настройки → Доступ к API):**\n"
            f"1. Отметьте галочками разделы **«Цены и скидки»** и **«Статистика»**.\n"
            f"2. ⚠️ Галочку **«Только на чтение» НЕ ставьте** — иначе бот сможет только смотреть цены, но не сможет добавлять ваши товары в акции по кнопке.",
            parse_mode="Markdown",
            reply_markup=get_cancel_kb(allow_back_to_mp=True)
        )
        await state.set_state(AddShopStates.waiting_for_wb_token)
    else:
        await message.answer(
            f"🔑 Отправьте данные **Ozon** для магазина *{shop_name}* через пробел:\n"
            f"`CLIENT_ID API_KEY`\n\n"
            f"📌 **Как получить ключ в ЛК Ozon Seller:**\n"
            f"1. Откройте **Настройки (⚙️) → API интеграции → Seller API**.\n"
            f"2. Скопируйте ваш **Client ID** и нажмите **«Сгенерировать ключ»**.\n"
            f"3. Выберите роли:\n"
            f"   • **«Product read-only»** (или **«Product»**) — чтение каталога и цен;\n"
            f"   • **«Actions»** — расчёт и участие в акциях _(либо **«Actions read-only»**, если нужно только смотреть акции)_;\n"
            f"   • Или выберите одну общую роль **«Admin»**.\n"
            f"ℹ️ _Подписка Ozon Premium Plus / Pro НЕ требуется — API цен и акций работает бесплатно._",
            parse_mode="Markdown",
            reply_markup=get_cancel_kb(allow_back_to_mp=True)
        )
        await state.set_state(AddShopStates.waiting_for_ozon_creds)


@router.message(AddShopStates.waiting_for_wb_token)
async def process_wb_token(message: Message, state: FSMContext):
    data = await state.get_data()
    is_new_shop = not bool(data.get("update_seller_id"))

    if not message.text:
        await message.answer(
            "⚠️ Отправьте токен текстовым сообщением.",
            reply_markup=get_cancel_kb(allow_back_to_mp=is_new_shop)
        )
        return

    if message.text.strip() in MENU_BUTTONS or message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Ввод токена отменен. Выберите нужный раздел в меню еще раз.")
        return

    token = message.text.strip()
    shop_name = data.get("shop_name", "Магазин WB")
    update_seller_id = data.get("update_seller_id")

    client = WBClient(token_prices=token, token_stats=token)
    is_valid, error_msg = await client.check_token_rights()

    if not is_valid:
        await message.answer(
            f"❌ **Ошибка проверки токена WB:**\n{error_msg}\n\n"
            f"Проверьте права токена и отправьте его повторно:",
            parse_mode="Markdown",
            reply_markup=get_cancel_kb(allow_back_to_mp=is_new_shop)
        )
        return

    is_stats_ok, _ = await client.check_stats_token()
    stats_token_val = token if is_stats_ok else None

    async with AsyncSessionFactory() as session:
        if update_seller_id:
            seller = (await session.execute(select(Seller).where(Seller.id == update_seller_id))).scalars().first()
            if seller:
                seller.wb_token_encrypted = encrypt_token(token)
                if stats_token_val:
                    seller.wb_stats_token_encrypted = encrypt_token(stats_token_val)
                seller_id = seller.id
        else:
            empty_seller = (await session.execute(
                select(Seller).where(
                    Seller.telegram_id == message.from_user.id,
                    Seller.wb_token_encrypted.is_(None),
                    Seller.ozon_api_key_encrypted.is_(None)
                )
            )).scalars().first()

            if empty_seller:
                empty_seller.shop_name = shop_name
                empty_seller.marketplace = "wb"
                empty_seller.wb_token_encrypted = encrypt_token(token)
                if stats_token_val:
                    empty_seller.wb_stats_token_encrypted = encrypt_token(stats_token_val)
                seller_id = empty_seller.id
            else:
                seller = Seller(
                    telegram_id=message.from_user.id,
                    shop_name=shop_name,
                    marketplace="wb",
                    wb_token_encrypted=encrypt_token(token),
                    wb_stats_token_encrypted=encrypt_token(stats_token_val) if stats_token_val else None
                )
                session.add(seller)
                await session.flush()
                seller_id = seller.id
                session.add(SellerSettings(seller_id=seller.id))

        await session.commit()

    sync_msg = await message.answer("⏳ Магазин подключен! Синхронизируем товары и выбираем флагманы...")
    added_cnt = 0
    flag_cnt = 0
    try:
        async with AsyncSessionFactory() as session:
            seller_obj = (await session.execute(select(Seller).where(Seller.id == seller_id))).scalars().first()
            if seller_obj:
                goods = await client.get_all_seller_goods()
                for g in goods:
                    sku = g["sku"]
                    ex = (await session.execute(select(Product).where(Product.seller_id == seller_id, Product.sku == sku))).scalars().first()
                    if not ex:
                        session.add(Product(seller_id=seller_id, marketplace="wb", sku=sku, title=g.get("vendor_code")))
                        added_cnt += 1
                await session.commit()

                if is_stats_ok:
                    top_map = await client.get_top_sales_skus(top_ratio=0.2)
                    if top_map:
                        await session.execute(update(Product).where(Product.seller_id == seller_id).values(is_flagship=False))
                        await session.execute(
                            update(Product).where(Product.seller_id == seller_id, Product.sku.in_(list(top_map.keys()))).values(is_flagship=True)
                        )
                        await session.commit()
                        flag_cnt = len(top_map)
    except Exception as e:
        print(f"Auto-sync error: {e}")

    await state.clear()
    await sync_msg.edit_text(
        f"✅ Магазин Wildberries **{shop_name}** успешно подключен и синхронизирован!\n\n"
        f"• Добавлено товаров: **{added_cnt} SKU**\n"
        f"• Назначено флагманов: **{flag_cnt} SKU**\n\n"
        f"Мониторинг цен и акций уже активен.",
        parse_mode="Markdown"
    )
    await message.answer(
        "👇 Используйте меню внизу для управления ботом:",
        reply_markup=get_main_keyboard(message.from_user.id)
    )


@router.message(AddShopStates.waiting_for_ozon_creds)
async def process_ozon_creds(message: Message, state: FSMContext):
    data = await state.get_data()
    is_new_shop = not bool(data.get("update_seller_id"))

    if not message.text:
        await message.answer(
            "⚠️ Отправьте `CLIENT_ID API_KEY` текстом.",
            parse_mode="Markdown",
            reply_markup=get_cancel_kb(allow_back_to_mp=is_new_shop)
        )
        return

    if message.text.strip() in MENU_BUTTONS or message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Подключение Ozon отменено. Выберите нужный раздел в меню еще раз.")
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer(
            "⚠️ Неверный формат. Отправьте `CLIENT_ID API_KEY` через пробел:",
            parse_mode="Markdown",
            reply_markup=get_cancel_kb(allow_back_to_mp=is_new_shop)
        )
        return

    client_id, api_key = parts[0], parts[1]
    shop_name = data.get("shop_name", "Магазин Ozon")
    update_seller_id = data.get("update_seller_id")

    client = OzonClient(client_id, api_key)
    is_valid, error_msg = await client.check_token_rights()

    if not is_valid:
        await message.answer(
            f"❌ **Ошибка подключения Ozon:**\n{error_msg}\n\n"
            f"Проверьте данные и отправьте снова:",
            parse_mode="Markdown",
            reply_markup=get_cancel_kb(allow_back_to_mp=is_new_shop)
        )
        return

    async with AsyncSessionFactory() as session:
        if update_seller_id:
            seller = (await session.execute(select(Seller).where(Seller.id == update_seller_id))).scalars().first()
            if seller:
                seller.ozon_client_id = client_id
                seller.ozon_api_key_encrypted = encrypt_token(api_key)
                seller_id = seller.id
        else:
            empty_seller = (await session.execute(
                select(Seller).where(
                    Seller.telegram_id == message.from_user.id,
                    Seller.wb_token_encrypted.is_(None),
                    Seller.ozon_api_key_encrypted.is_(None)
                )
            )).scalars().first()

            if empty_seller:
                empty_seller.shop_name = shop_name
                empty_seller.marketplace = "ozon"
                empty_seller.ozon_client_id = client_id
                empty_seller.ozon_api_key_encrypted = encrypt_token(api_key)
                seller_id = empty_seller.id
            else:
                seller = Seller(
                    telegram_id=message.from_user.id,
                    shop_name=shop_name,
                    marketplace="ozon",
                    ozon_client_id=client_id,
                    ozon_api_key_encrypted=encrypt_token(api_key)
                )
                session.add(seller)
                await session.flush()
                seller_id = seller.id
                session.add(SellerSettings(seller_id=seller.id))

        await session.commit()

    sync_msg = await message.answer("⏳ Магазин Ozon подключен! Синхронизируем каталог...")
    added_cnt = 0
    try:
        async with AsyncSessionFactory() as session:
            items = await client.get_all_product_ids()
            for item in items:
                sku_val = str(item["sku"])
                ex = (await session.execute(select(Product).where(Product.seller_id == seller_id, Product.sku == sku_val))).scalars().first()
                if not ex:
                    session.add(Product(seller_id=seller_id, marketplace="ozon", sku=sku_val, title=str(item.get("offer_id", ""))))
                    added_cnt += 1
            await session.commit()
    except Exception as e:
        print(f"Ozon auto-sync error: {e}")

    await state.clear()
    await sync_msg.edit_text(
        f"✅ Магазин Ozon **{shop_name}** успешно подключен и синхронизирован!\n"
        f"• Загружено товаров: **{added_cnt} SKU**\n\n"
        f"Мониторинг цен активен.",
        parse_mode="Markdown"
    )
    await message.answer(
        "👇 Используйте меню внизу для управления ботом:",
        reply_markup=get_main_keyboard(message.from_user.id)
    )


@router.callback_query(F.data.startswith("del_shop_"))
async def cb_delete_shop(callback: CallbackQuery):
    shop_id = int(callback.data.split("_")[-1])
    async with AsyncSessionFactory() as session:
        seller = (await session.execute(
            select(Seller).where(Seller.id == shop_id, Seller.telegram_id == callback.from_user.id)
        )).scalars().first()

    if not seller:
        await callback.answer("Магазин уже удален")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_del_shop_{shop_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_del_shop")]
    ])
    try:
        await callback.message.edit_text(
            f"⚠️ Удалить магазин **{clean_md(seller.shop_name)}** вместе со всеми его товарами?",
            parse_mode="Markdown",
            reply_markup=kb
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_del_shop_"))
async def cb_confirm_delete_shop(callback: CallbackQuery):
    shop_id = int(callback.data.split("_")[-1])
    async with AsyncSessionFactory() as session:
        seller = (await session.execute(
            select(Seller).where(Seller.id == shop_id, Seller.telegram_id == callback.from_user.id)
        )).scalars().first()

        if seller:
            shop_title = clean_md(seller.shop_name)
            await session.execute(delete(Product).where(Product.seller_id == seller.id))
            await session.delete(seller)
            await session.commit()
            try:
                await callback.message.edit_text(f"🗑 Магазин **{shop_title}** и все его товары удалены из мониторинга.", parse_mode="Markdown")
            except Exception:
                pass
            await callback.answer("Магазин удален")
        else:
            await callback.answer("Магазин уже удален")


@router.callback_query(F.data == "cancel_del_shop")
async def cb_cancel_delete_shop(callback: CallbackQuery):
    try:
        await callback.message.edit_text("❌ Удаление отменено.")
    except Exception:
        pass
    await callback.answer()


# --- Раздел «Мои товары» ---

@router.message(Command("products"))
@router.message(F.text == "📊 Мои товары")
async def cmd_my_products(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).options(selectinload(Seller.products)).where(Seller.telegram_id == message.from_user.id)
        )).scalars().all()

        shop_lines = []
        total_sku = 0
        stop_price_sku = 0
        for s in sellers:
            if s.wb_token_encrypted or s.ozon_api_key_encrypted or s.products:
                mp_ico = "🟣 WB" if s.marketplace == "wb" else "🔵 Ozon"
                cnt = len(s.products)
                total_sku += cnt
                stop_cnt = sum(1 for p in s.products if (p.min_price or 0) > 0)
                stop_price_sku += stop_cnt
                shop_lines.append(f"• {mp_ico} **{clean_md(s.shop_name)}**: `{cnt} SKU` (со стоп-ценой: `{stop_cnt}`)")

        shops_summary = "\n".join(shop_lines) if shop_lines else "• Нет синхронизированных магазинов"

        rows = [
            [InlineKeyboardButton(text="⚡️ Проверить цены и СПП сейчас", callback_data="run_price_check")],
            [InlineKeyboardButton(text="🔎 Найти товар по артикулу (SKU)", callback_data="prompt_sku_search")],
            [InlineKeyboardButton(text="📥 Скачать прайс-лист (по кабинетам CSV)", callback_data="export_current_prices_csv")],
            [InlineKeyboardButton(text="🛑 Задать стоп-цены (текстом или CSV)", callback_data="upload_min_prices_csv")],
        ]
        if stop_price_sku > 0:
            rows.append([InlineKeyboardButton(text=f"🗑 Очистить все стоп-цены ({stop_price_sku} SKU)", callback_data="clear_min_prices_ask")])

        rows.extend([
            [InlineKeyboardButton(text="🔄 Синхронизировать каталоги магазинов", callback_data="sync_all_products")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")]
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        await message.answer(
            f"📊 **Управление каталогом и ценами**\n\n"
            f"{shops_summary}\n\n"
            f"📦 Всего под защитой: **{total_sku} SKU**\n"
            f"🛑 Задана стоп-цена: **{stop_price_sku} SKU**\n\n"
            f"💡 _Быстрый поиск: вы можете в любой момент просто отправить артикул (SKU) или код продавца в чат!_",
            reply_markup=kb,
            parse_mode="Markdown"
        )


@router.callback_query(F.data == "prompt_sku_search")
async def cb_prompt_sku_search(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "🔎 **Быстрый поиск товара**\n\n"
        "Просто отправьте в чат **артикул маркетплейса (SKU)** или **артикул продавца** (например: `995598603` или `ANBNITRIL`).\n"
        "Бот мгновенно покажет карточку с текущими ценами, СПП и вашей Стоп-ценой.",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "upload_min_prices_csv")
async def cb_upload_min_prices(callback: CallbackQuery, state: FSMContext):
    async with AsyncSessionFactory() as session:
        stop_cnt = (await session.execute(
            select(func.count(Product.id)).join(Seller).where(
                Seller.telegram_id == callback.from_user.id,
                Product.min_price > 0
            )
        )).scalar() or 0

    rows = [
        [InlineKeyboardButton(text="📥 Скачать шаблон прайс-листа (CSV)", callback_data="export_current_prices_csv")],
    ]
    if stop_cnt > 0:
        rows.append([InlineKeyboardButton(text=f"🗑 Очистить все стоп-цены ({stop_cnt} SKU)", callback_data="clear_min_prices_ask")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_min_prices_csv")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await callback.message.answer(
        "🛑 **Установка стоп-цен (минимальных цен рентабельности)**\n\n"
        "**Способ 1 — Быстро текстом со смартфона:**\n"
        "Просто отправьте сюда сообщение в формате `АРТИКУЛ СТОП_ЦЕНА` (каждый товар с новой строки). Например:\n"
        "`15489231 1250`\n"
        "`98412054 890`\n"
        "_(чтобы убрать стоп-цену у одного артикула, отправьте `АРТИКУЛ 0`)_\n\n"
        "**Способ 2 — Массово через Excel (CSV):**\n"
        "1. Нажмите **«📥 Скачать шаблон прайс-листа»** и выберите нужный кабинет.\n"
        "2. Заполните последнюю колонку **«Стоп-цена»** в Excel, сохраните и отправьте файл `.csv` сюда документом.",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await state.set_state(AddShopStates.waiting_for_min_prices_csv)
    await callback.answer()


@router.callback_query(F.data == "clear_min_prices_ask")
async def cb_clear_min_prices_ask(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, сбросить все стоп-цены", callback_data="clear_min_prices_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_min_prices_csv")]
    ])
    try:
        await callback.message.edit_text(
            "⚠️ **Вы уверены, что хотите обнулить Стоп-цены у всех ваших товаров?**\n\n"
            "После сброса товары не будут блокироваться по минимальному порогу цены при входе в акции.",
            parse_mode="Markdown",
            reply_markup=kb
        )
    except Exception:
        await callback.message.answer(
            "⚠️ **Вы уверены, что хотите обнулить Стоп-цены у всех ваших товаров?**",
            parse_mode="Markdown",
            reply_markup=kb
        )
    await callback.answer()


@router.callback_query(F.data == "clear_min_prices_confirm")
async def cb_clear_min_prices_confirm(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller.id).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()

        if sellers:
            await session.execute(
                update(Product)
                .where(Product.seller_id.in_(sellers), Product.min_price > 0)
                .values(min_price=0.0)
            )
            await session.commit()

    try:
        await callback.message.edit_text("🗑 **Все стоп-цены успешно очищены (сброшены до 0 ₽).**", parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer("✅ Стоп-цены очищены")


@router.callback_query(F.data == "cancel_min_prices_csv")
async def cb_cancel_min_prices(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Установка стоп-цен отменена.")
    except Exception:
        pass
    await callback.answer()


@router.message(AddShopStates.waiting_for_min_prices_csv, F.text)
async def process_min_prices_text(message: Message, state: FSMContext):
    """Быстрая установка стоп-цен обычным текстовым сообщением (АРТИКУЛ ЦЕНА)."""
    text = (message.text or "").strip()
    if text in MENU_BUTTONS or text.startswith("/"):
        await state.clear()
        await message.answer("❌ Установка стоп-цен отменена. Выберите нужный раздел в меню еще раз.")
        return

    updates_map = {}
    for line in text.splitlines():
        line_clean = line.strip().replace(";", " ").replace(",", ".").replace("₽", "").replace("=", " ").replace("-", " ")
        parts = [p for p in line_clean.split() if p]
        if len(parts) >= 2:
            raw_sku = parts[0].strip()
            try:
                val = float(parts[1])
                if val >= 0:
                    updates_map[raw_sku] = val
            except ValueError:
                continue

    if not updates_map:
        await message.answer(
            "⚠️ Не удалось распознать формат. Отправьте `АРТИКУЛ СТОП_ЦЕНА` через пробел (например: `15489231 1250`) или прикрепите `.csv` файл.",
            parse_mode="Markdown"
        )
        return

    updated_count = 0
    not_found = []
    async with AsyncSessionFactory() as session:
        products = (await session.execute(
            select(Product).join(Seller).where(Seller.telegram_id == message.from_user.id)
        )).scalars().all()
        prod_by_sku = {p.sku: p for p in products}

        for sku_key, new_min in updates_map.items():
            if sku_key in prod_by_sku:
                prod_by_sku[sku_key].min_price = new_min
                updated_count += 1
            else:
                not_found.append(sku_key)
        await session.commit()

    await state.clear()
    nf_note = f"\n⚠️ Не найдены в вашем каталоге: `{', '.join(not_found[:10])}`" if not_found else ""
    await message.answer(
        f"✅ **Стоп-цены успешно сохранены!**\n\n"
        f"• Обновлено товаров в базе: **{updated_count} SKU**{nf_note}\n\n"
        f"Теперь эти артикулы защищены от невыгодных акций.",
        parse_mode="Markdown"
    )


@router.message(AddShopStates.waiting_for_min_prices_csv, F.document)
async def process_min_prices_csv(message: Message, state: FSMContext):
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".csv"):
        await message.answer("⚠️ Пожалуйста, отправьте файл в формате **.csv** (разделитель `;` или `,`).", parse_mode="Markdown")
        return

    status_msg = await message.answer("⏳ Обрабатываем CSV-файл и обновляем стоп-цены...")

    try:
        file_io = io.BytesIO()
        await message.bot.download(doc, destination=file_io)
        raw_bytes = file_io.getvalue()

        try:
            text_content = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text_content = raw_bytes.decode("cp1251", errors="ignore")

        lines = [line for line in text_content.splitlines() if line.strip()]
        if not lines:
            await status_msg.edit_text("⚠️ Файл пуст.")
            return

        delimiter = ";" if ";" in lines[0] else ","
        reader = csv.reader(lines, delimiter=delimiter)
        headers = [h.strip().lower() for h in next(reader, [])]

        sku_idx = next((i for i, h in enumerate(headers) if "артикул (sku)" in h or "product id" in h or h == "sku" or h == "артикул"), 2)
        min_p_idx = next((i for i, h in enumerate(headers) if "стоп" in h or "мин" in h or "min" in h), None)

        if min_p_idx is None:
            if len(headers) == 2:
                sku_idx, min_p_idx = 0, 1
            else:
                min_p_idx = len(headers) - 1

        updates_map = {}
        for row in reader:
            if len(row) <= max(sku_idx, min_p_idx):
                continue
            raw_sku = row[sku_idx].strip().replace('="', '').replace('"', '').replace("'", "").strip()
            raw_min = row[min_p_idx].strip().replace("₽", "").replace(" ", "").replace(",", ".")
            if not raw_sku:
                continue
            try:
                val = float(raw_min) if raw_min else 0.0
                if val >= 0:
                    updates_map[raw_sku] = val
            except ValueError:
                continue

        updated_count = 0
        async with AsyncSessionFactory() as session:
            products = (await session.execute(
                select(Product).join(Seller).where(Seller.telegram_id == message.from_user.id)
            )).scalars().all()

            for p in products:
                if p.sku in updates_map:
                    new_min = updates_map[p.sku]
                    if (p.min_price or 0.0) != new_min:
                        p.min_price = new_min
                        updated_count += 1
            await session.commit()

        await state.clear()
        await status_msg.edit_text(
            f"✅ **Стоп-цены успешно обновлены!**\n\n"
            f"• Распознано позиций в файле: **{len(updates_map)}**\n"
            f"• Обновлено стоп-цен в базе: **{updated_count} SKU**\n\n"
            f"Теперь при анализе акций WB и Ozon эти товары защищены от входа ниже минимальной цены.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка чтения файла: `{e}`", parse_mode="Markdown")


@router.callback_query(F.data.in_(["run_price_check", "force_check_prices"]))
async def cb_force_check(callback: CallbackQuery):
    acc = await ensure_user_account(callback.from_user.id)
    if acc.is_banned or not acc.subscription_until or acc.subscription_until < datetime.utcnow():
        await callback.answer("Подписка истекла!", show_alert=True)
        return

    await callback.answer("Запускаем сбор цен и СПП с витрины...")
    status_msg = await callback.message.answer("⏳ Запущен опрос витрины по всем магазинам. Пожалуйста, подождите...")
    changes = await check_prices(callback.bot, flagship_only=False, target_user_id=callback.from_user.id)

    try:
        if changes == -1:
            await status_msg.edit_text("⚠️ Опрос по вашему аккаунту уже выполняется. Дождитесь его завершения.")
        elif changes == 0:
            await status_msg.edit_text("✅ Проверка завершена. Все актуальные цены и СПП записаны в базу (новых отклонений нет).")
        else:
            await status_msg.edit_text(f"✅ Проверка завершена. Найдено и отправлено изменений: {changes}.")
    except Exception:
        pass


@router.callback_query(F.data == "export_current_prices_csv")
async def cb_export_csv_menu(callback: CallbackQuery):
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).options(selectinload(Seller.products)).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()

    active_sellers = [s for s in sellers if s.products]
    if not active_sellers:
        await callback.answer("В базе пока нет товаров", show_alert=True)
        await callback.message.answer("⚠️ В базе пока нет товаров. Сначала нажмите «🔄 Синхронизировать каталоги магазинов».")
        return

    # Если у селлера всего 1 магазин — скачиваем его сразу без лишнего клика
    if len(active_sellers) == 1:
        await _send_seller_csv(callback, active_sellers, single_shop=active_sellers[0])
        return

    # Если магазинов несколько — даем удобный выбор по каждому кабинету отдельно
    buttons = []
    total_all = 0
    for s in active_sellers:
        mp_ico = "🟣 WB" if s.marketplace == "wb" else "🔵 Ozon"
        cnt = len(s.products)
        total_all += cnt
        buttons.append([InlineKeyboardButton(
            text=f"📥 {mp_ico} | {s.shop_name} ({cnt} SKU)",
            callback_data=f"export_shop_csv_{s.id}"
        )])

    buttons.append([InlineKeyboardButton(
        text=f"📦 Скачать все кабинеты одним файлом ({total_all} SKU)",
        callback_data="export_shop_csv_all"
    )])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_products_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer(
        "📥 **Выгрузка прайс-листа (Excel CSV)**\n\n"
        "Выберите кабинет, для которого нужно сформировать таблицу цен и стоп-цен:",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_products_menu")
async def cb_back_to_products_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await cmd_my_products(callback.message, state)


@router.callback_query(F.data.startswith("export_shop_csv_"))
async def cb_export_shop_csv_execute(callback: CallbackQuery):
    target = callback.data.replace("export_shop_csv_", "")
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).options(selectinload(Seller.products)).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()

    if target == "all":
        active_sellers = [s for s in sellers if s.products]
        await _send_seller_csv(callback, active_sellers, single_shop=None)
    else:
        shop_id = int(target)
        shop = next((s for s in sellers if s.id == shop_id), None)
        if not shop or not shop.products:
            await callback.answer("Магазин не найден или пуст", show_alert=True)
            return
        await _send_seller_csv(callback, [shop], single_shop=shop)


async def _send_seller_csv(callback: CallbackQuery, sellers_list: list[Seller], single_shop: Seller | None = None):
    await callback.answer("Формируем CSV-таблицу...")
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")

    is_only_ozon = single_shop and single_shop.marketplace == "ozon"

    if is_only_ozon:
        writer.writerow([
            "Магазин",
            "Маркетплейс",
            "Артикул Ozon (SKU)",
            "Артикул продавца (Offer ID)",
            "Флагман (Топ-20%)",
            "Цена продавца / FBO (₽)",
            "Цена клиента без Ozon Карты (₽)",
            "Цена клиента с Ozon Картой (₽)",
            "Соинвест / Скидка Ozon (%)",
            "Стоп-цена (Мин. цена, ₽)"
        ])
    else:
        writer.writerow([
            "Магазин",
            "Маркетплейс",
            "Артикул (SKU)",
            "Артикул продавца / Название",
            "Флагман (Топ-20%)",
            "Цена продавца (₽)",
            "Цена клиента без кошелька/карты (₽)",
            "Цена клиента с Кошельком / Ozon Картой (₽)",
            "Текущая СПП / Соинвест (%)",
            "Стоп-цена (Мин. цена, ₽)"
        ])

    rows_count = 0
    for s in sellers_list:
        mp_label = "Wildberries" if s.marketplace == "wb" else "Ozon"
        for p in s.products:
            rows_count += 1
            sp = p.last_seller_price or 0.0
            cp = p.last_client_price or sp
            wp = p.last_wallet_price or (round(cp * 0.915) if s.marketplace == "ozon" and cp > 0 else cp)
            spp_val = p.last_spp or (round(((sp - wp) / sp) * 100) if s.marketplace == "ozon" and sp > wp > 0 else 0)

            writer.writerow([
                csv_safe(s.shop_name),
                mp_label,
                f'="{p.sku}"',
                csv_safe(p.title or p.sku),
                "Да" if p.is_flagship else "Нет",
                f"{sp:.2f}".replace(".", ","),
                f"{cp:.2f}".replace(".", ","),
                f"{wp:.2f}".replace(".", ","),
                f"{spp_val}%",
                f"{p.min_price or 0:.2f}".replace(".", ",")
            ])

    csv_bytes = output.getvalue().encode("utf-8")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    if single_shop:
        safe_slug = "".join(c if c.isalnum() else "_" for c in (single_shop.shop_name or "shop"))[:20]
        filename = f"{single_shop.marketplace}_{safe_slug}_{timestamp}.csv"
        title_line = f"📥 **Прайс-лист кабинета {clean_md(single_shop.shop_name)} ({single_shop.marketplace.upper()})**"
    else:
        filename = f"all_shops_catalog_{timestamp}.csv"
        title_line = "📥 **Общий прайс-лист по всем магазинам**"

    document = BufferedInputFile(csv_bytes, filename=filename)

    await callback.message.answer_document(
        document=document,
        caption=(
            f"{title_line}\n"
            f"Всего позиций: **{rows_count} SKU**\n\n"
            f"💡 _Вы можете заполнить колонку «Стоп-цена» в этом файле и загрузить его обратно через кнопку «🛑 Задать стоп-цены»._"
        ),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "sync_all_products")
async def cb_sync_all_products(callback: CallbackQuery):
    await callback.answer("Запускаем синхронизацию каталогов...")
    status_msg = await callback.message.answer("⏳ Синхронизируем каталоги всех подключенных магазинов...")

    total_added = 0
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()

        for seller in sellers:
            if seller.marketplace == "wb" and seller.wb_token_encrypted:
                token = decrypt_token(seller.wb_token_encrypted)
                client = WBClient(token_prices=token)
                try:
                    goods = await client.get_all_seller_goods()
                    for g in goods:
                        sku = str(g["sku"])
                        ex = (await session.execute(
                            select(Product).where(Product.seller_id == seller.id, Product.sku == sku)
                        )).scalars().first()
                        if not ex:
                            session.add(Product(
                                seller_id=seller.id,
                                marketplace="wb",
                                sku=sku,
                                title=g.get("vendor_code")
                            ))
                            total_added += 1
                    await session.commit()
                except Exception as e:
                    print(f"Sync error WB seller {seller.id}: {e}")

            elif seller.marketplace == "ozon" and seller.ozon_api_key_encrypted:
                key = decrypt_token(seller.ozon_api_key_encrypted)
                ozon = OzonClient(seller.ozon_client_id, key)
                try:
                    items = await ozon.get_all_product_ids()
                    for item in items:
                        sku_val = str(item["sku"])
                        ex = (await session.execute(
                            select(Product).where(Product.seller_id == seller.id, Product.sku == sku_val)
                        )).scalars().first()
                        if not ex:
                            session.add(Product(
                                seller_id=seller.id,
                                marketplace="ozon",
                                sku=sku_val,
                                title=str(item.get("offer_id", ""))
                            ))
                            total_added += 1
                    await session.commit()
                except Exception as e:
                    print(f"Sync error Ozon seller {seller.id}: {e}")

    try:
        await status_msg.edit_text(
            f"✅ **Синхронизация каталогов завершена!**\n\n"
            f"• Новых товаров добавлено в базу: **{total_added} SKU**",
            parse_mode="Markdown"
        )
    except Exception:
        pass


# --- Раздел «Флагманы» ---

@router.message(Command("flagships"))
@router.message(F.text == "🎯 Флагманы")
async def show_flagships_menu(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionFactory() as session:
        flagships = (await session.execute(
            select(Product).join(Seller).where(
                Seller.telegram_id == message.from_user.id,
                Product.is_flagship == True
            )
        )).scalars().all()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪄 Автовыбор топ-20% (по заказам за 30 дней)", callback_data="auto_pick_flagships")],
        [InlineKeyboardButton(text="🗑 Сбросить все флагманы", callback_data="reset_flagships")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")]
    ])

    if flagships:
        sku_list = "\n".join([f"• `{p.sku}` ({clean_md(p.title) or 'Без названия'})" for p in flagships[:15]])
        more_txt = f"\n... и еще {len(flagships) - 15} SKU" if len(flagships) > 15 else ""
        text = (
            f"🎯 **Ваши товары-флагманы ({len(flagships)} SKU)**\n"
            f"• Проверяются в **4 раза чаще** (каждые 15 минут)\n"
            f"• Автоматически пересчитываются в 03:00 МСК по реальным заказам за 30 дней\n\n"
            f"{sku_list}{more_txt}"
        )
    else:
        text = (
            "🎯 **Флагманы пока не назначены**\n\n"
            "Флагманы — это ваши главные товары-локомотивы, которые приносят основную выручку. "
            "Бот проверяет их цены и СПП каждые 15 минут.\n\n"
            "Нажмите **«🪄 Автовыбор топ-20%»**, чтобы бот сам выделил лидеров по реальным заказам за 30 дней."
        )

    await message.answer(text, parse_mode="Markdown", reply_markup=markup)


@router.callback_query(F.data.in_(["auto_flagships", "auto_pick_flagships"]))
async def cb_auto_pick_flagships(callback: CallbackQuery):
    await callback.answer("Считаем заказы за последние 30 дней...")
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()

        total_flagships = 0
        stats_errors = []
        for seller in sellers:
            if seller.marketplace == "wb" and seller.wb_token_encrypted:
                stats_token = decrypt_token(seller.wb_stats_token_encrypted) if seller.wb_stats_token_encrypted else decrypt_token(seller.wb_token_encrypted)
                client = WBClient(token_stats=stats_token)
                top_map = await client.get_top_sales_skus(top_ratio=0.2)
                if top_map:
                    await session.execute(update(Product).where(Product.seller_id == seller.id).values(is_flagship=False))
                    await session.execute(
                        update(Product)
                        .where(Product.seller_id == seller.id, Product.sku.in_(list(top_map.keys())))
                        .values(is_flagship=True)
                    )
                    total_flagships += len(top_map)
                else:
                    stats_errors.append(
                        f"• **{clean_md(seller.shop_name)}**: нет доступа к заказам (проверьте галочку **«Статистика»** у токена WB)."
                    )
        await session.commit()

    text = f"✅ Флагманы пересчитаны по реальным заказам! Всего назначено: **{total_flagships} SKU**."
    if stats_errors:
        text += "\n\n⚠️ **Предупреждения:**\n" + "\n".join(stats_errors)

    await callback.message.answer(text, parse_mode="Markdown")


@router.callback_query(F.data == "reset_flagships")
async def cb_reset_flagships(callback: CallbackQuery):
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()
        for s in sellers:
            await session.execute(update(Product).where(Product.seller_id == s.id).values(is_flagship=False))
        await session.commit()
    await callback.answer("Флагманы сброшены")
    try:
        await callback.message.edit_text("🗑 Все флагманы сброшены. Товары опрашиваются раз в 60 минут.")
    except Exception:
        pass


# --- Быстрый поиск товара по артикулу (SKU) или названию прямо в чате ---

@router.callback_query(F.data.startswith("quick_stop_"))
async def cb_quick_set_stop(callback: CallbackQuery, state: FSMContext):
    sku = callback.data.replace("quick_stop_", "")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_min_prices_csv")]
    ])
    await callback.message.answer(
        f"🛑 Введите новую Стоп-цену для артикула `{sku}` в формате:\n"
        f"`{sku} ЦЕНА` (например: `{sku} 1850`)",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await state.set_state(AddShopStates.waiting_for_min_prices_csv)
    await callback.answer()


@router.callback_query(F.data.startswith("quick_clear_stop_"))
async def cb_quick_clear_stop(callback: CallbackQuery):
    sku = callback.data.replace("quick_clear_stop_", "")
    async with AsyncSessionFactory() as session:
        product = (await session.execute(
            select(Product).join(Seller).where(
                Seller.telegram_id == callback.from_user.id,
                Product.sku == sku
            ).limit(1)
        )).scalars().first()
        if product:
            product.min_price = 0.0
            await session.commit()

    await callback.answer(f"🗑 Стоп-цена для {sku} сброшена!", show_alert=True)
    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n🗑 _Стоп-цена для артикула {sku} сброшена до 0 ₽._",
            parse_mode="Markdown"
        )
    except Exception:
        pass


@router.message(F.text, ~F.text.in_(MENU_BUTTONS), ~F.text.startswith("/"))
async def quick_sku_lookup(message: Message, state: FSMContext):
    """
    Если пользователь вне состояний FSM отправляет артикул (SKU) или код продавца,
    бот мгновенно показывает карточку товара с ценами, СПП и Стоп-ценой.
    """
    current_state = await state.get_state()
    if current_state is not None:
        return

    query = (message.text or "").strip()
    if not query or query in MENU_BUTTONS or query.startswith("/"):
        return

    async with AsyncSessionFactory() as session:
        product = (await session.execute(
            select(Product).options(selectinload(Product.seller)).join(Seller).where(
                Seller.telegram_id == message.from_user.id,
                (Product.sku == query) | (Product.title.ilike(f"%{query}%"))
            ).limit(1)
        )).scalars().first()

    if not product:
        if query.isdigit():
            await message.answer(
                f"🔍 Артикул `{query}` не найден в базе ваших магазинов.\n"
                f"Если это новый товар, нажмите «🔄 Синхронизировать каталоги магазинов» в разделе 📊 Мои товары.",
                parse_mode="Markdown"
            )
        return

    shop_name = clean_md(product.seller.shop_name) if product.seller else "Магазин"
    is_wb = (product.marketplace == "wb")
    mp_ico = "🟣 WB" if is_wb else "🔵 Ozon"
    flag_str = "🎯 Да (опрос каждые 15 мин)" if product.is_flagship else "Нет (опрос раз в 60 мин)"

    sp = round(product.last_seller_price or 0)
    cp = round(product.last_client_price or sp)
    wp = round(product.last_wallet_price or cp)
    spp = int(product.last_spp or 0)
    min_p = round(product.min_price or 0)

    if min_p > 0:
        if 0 < sp < min_p:
            stop_status = f"**{min_p} ₽** 🚨 _(ВНИМАНИЕ: текущая цена на {min_p - sp} ₽ ниже Стоп-цены!)_"
        else:
            stop_status = f"**{min_p} ₽** ✅ _(запас маржи: +{sp - min_p} ₽)_"
    else:
        stop_status = "не задана _(рекомендуем установить)_"

    ext_url = f"https://www.wildberries.ru/catalog/{product.sku}/detail.aspx" if is_wb else f"https://www.ozon.ru/product/{product.sku}/"

    price_lines = [
        f"• 👨‍💻 Цена продавца: **{sp} ₽**",
        f"• 🛍 Цена клиента (на витрине): **{cp} ₽**",
    ]
    if is_wb:
        price_lines.append(f"• 💳 Цена с WB Кошельком: **{wp} ₽**")
    price_lines.append(f"• 🟡 {'Текущая СПП' if is_wb else 'Соинвест Ozon'}: **{spp}%**")

    text = (
        f"🔎 **Карточка товара | {mp_ico} ({shop_name})**\n\n"
        f"📦 Артикул (SKU): `{product.sku}`\n"
        f"🏷 Артикул продавца: **{clean_md(product.title) or '—'}**\n"
        f"⭐️ Флагман: {flag_str}\n\n"
        f"💰 **Текущие цены и скидки:**\n"
        + "\n".join(price_lines) + "\n\n"
        f"🛑 **Ваша Стоп-цена:** {stop_status}"
    )

    kb_rows = [
        [InlineKeyboardButton(text=f"🌐 Открыть на {'Wildberries' if is_wb else 'Ozon'}", url=ext_url)],
        [InlineKeyboardButton(text="🛑 Изменить Стоп-цену", callback_data=f"quick_stop_{product.sku}")],
    ]
    if min_p > 0:
        kb_rows.append([InlineKeyboardButton(text="🗑 Сбросить Стоп-цену", callback_data=f"quick_clear_stop_{product.sku}")])
    kb_rows.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await message.answer(text, parse_mode="Markdown", reply_markup=kb)
