from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from database import AsyncSessionFactory
from models import Seller, SellerSettings, UserAccount, Product
from handlers.base import ensure_user_account

router = Router()


async def _load(tg_id: int):
    await ensure_user_account(tg_id)
    async with AsyncSessionFactory() as session:
        acc = (await session.execute(
            select(UserAccount).where(UserAccount.telegram_id == tg_id)
        )).scalars().first()
        sellers = (await session.execute(
            select(Seller).options(selectinload(Seller.settings)).where(Seller.telegram_id == tg_id)
        )).scalars().all()
        first = sellers[0] if sellers else None
        spp = first.settings.spp_delta_threshold if first and first.settings else 1
        price = int(first.settings.price_delta_threshold) if first and first.settings else 1
        auto_join = first.settings.auto_join_promos if first and first.settings else False
        seller_id = first.id if first else None
    return acc, spp, price, auto_join, seller_id


def _text(spp: int, price: int, auto_join: bool, acc) -> str:
    n_spp = acc.notify_spp if acc else True
    n_price = acc.notify_price if acc else True
    n_promos = acc.notify_promos if acc else True
    auto_status = "🤖 Автоматический (скидка ≤ 8% и не ниже Стоп-цены)" if auto_join else "👤 Ручной (по кнопке)"
    return (
        "⚙️ **Настройки мониторинга и уведомлений**\n\n"
        "📊 **Чувствительность детектора:**\n"
        f"• Изменение СПП / Соинвеста: **от {spp}%**\n"
        f"• Изменение цены продавца: **от {price} ₽**\n\n"
        f"⚡️ **Режим входа в акции (для всех магазинов):**\n`{auto_status}`\n\n"
        "🔔 **Активные каналы уведомлений:**\n"
        f"• {'✅ Включены' if n_spp else '❌ Выключены'} — изменения СПП\n"
        f"• {'✅ Включены' if n_price else '❌ Выключены'} — изменения цены продавца\n"
        f"• {'✅ Включены' if n_promos else '❌ Выключены'} — новые акции и напоминания\n\n"
        "👇 _Нажмите на нужную кнопку ниже для переключения:_"
    )


def _keyboard(spp: int, price: int, auto_join: bool, seller_id: int | None, acc) -> InlineKeyboardMarkup:
    n_spp = acc.notify_spp if acc else True
    n_price = acc.notify_price if acc else True
    n_promos = acc.notify_promos if acc else True

    spp_options = [1, 2, 3, 5]
    price_options = [1, 10, 50, 100]

    spp_row = [
        InlineKeyboardButton(
            text=f"{'🔘' if spp == v else '⚪️'} СПП {v}%",
            callback_data=f"set_spp_{v}"
        ) for v in spp_options
    ]
    price_row = [
        InlineKeyboardButton(
            text=f"{'🔘' if price == v else '⚪️'} {v} ₽",
            callback_data=f"set_price_{v}"
        ) for v in price_options
    ]

    auto_status_txt = "🤖 Автоматический (≤ 8%)" if auto_join else "👤 Ручной"

    return InlineKeyboardMarkup(inline_keyboard=[
        spp_row,
        price_row,
        [
            InlineKeyboardButton(
                text=f"⚙️ Вход в акции: {auto_status_txt}",
                callback_data="settings_toggle_promos_mode"
            )
        ],
        [
            InlineKeyboardButton(text=f"СПП: {'🔔 Вкл' if n_spp else '🔕 Выкл'}", callback_data="toggle_spp"),
            InlineKeyboardButton(text=f"Цены: {'🔔 Вкл' if n_price else '🔕 Выкл'}", callback_data="toggle_price"),
            InlineKeyboardButton(text=f"Акции: {'🔔 Вкл' if n_promos else '🔕 Выкл'}", callback_data="toggle_promos"),
        ],
        [InlineKeyboardButton(text="❓ Ответы на частые вопросы (FAQ)", callback_data="goto_faq")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")]
    ])


@router.message(Command("settings"))
@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message):
    acc, spp, price, auto_join, seller_id = await _load(message.from_user.id)
    await message.answer(
        _text(spp, price, auto_join, acc),
        parse_mode="Markdown",
        reply_markup=_keyboard(spp, price, auto_join, seller_id, acc)
    )


@router.callback_query(F.data.startswith("set_spp_"))
async def cb_set_spp(callback: CallbackQuery):
    val = int(callback.data.split("_")[-1])
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).options(selectinload(Seller.settings)).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()

        if not sellers:
            await callback.answer("⚠️ Сначала подключите магазин в разделе «🏪 Мои магазины»!", show_alert=True)
            return

        for s in sellers:
            if not s.settings:
                s.settings = SellerSettings(seller_id=s.id, spp_delta_threshold=val)
                session.add(s.settings)
            else:
                s.settings.spp_delta_threshold = val
        await session.commit()

    acc, spp, price, auto_join, seller_id = await _load(callback.from_user.id)
    try:
        await callback.message.edit_text(
            _text(spp, price, auto_join, acc),
            parse_mode="Markdown",
            reply_markup=_keyboard(spp, price, auto_join, seller_id, acc)
        )
    except Exception:
        pass
    await callback.answer(f"✅ Порог СПП установлен: от {val}%")


@router.callback_query(F.data.startswith("set_price_"))
async def cb_set_price(callback: CallbackQuery):
    val = float(callback.data.split("_")[-1])
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).options(selectinload(Seller.settings)).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()

        if not sellers:
            await callback.answer("⚠️ Сначала подключите магазин в разделе «🏪 Мои магазины»!", show_alert=True)
            return

        for s in sellers:
            if not s.settings:
                s.settings = SellerSettings(seller_id=s.id, price_delta_threshold=val)
                session.add(s.settings)
            else:
                s.settings.price_delta_threshold = val
        await session.commit()

    acc, spp, price, auto_join, seller_id = await _load(callback.from_user.id)
    try:
        await callback.message.edit_text(
            _text(spp, price, auto_join, acc),
            parse_mode="Markdown",
            reply_markup=_keyboard(spp, price, auto_join, seller_id, acc)
        )
    except Exception:
        pass
    await callback.answer(f"✅ Порог цены установлен: от {int(val)} ₽")


@router.callback_query(F.data == "settings_toggle_promos_mode")
async def cb_toggle_auto_promo(callback: CallbackQuery):
    async with AsyncSessionFactory() as session:
        sellers = (await session.execute(
            select(Seller).options(selectinload(Seller.settings)).where(Seller.telegram_id == callback.from_user.id)
        )).scalars().all()

        if not sellers:
            await callback.answer("⚠️ Сначала подключите магазин в разделе «🏪 Мои магазины»!", show_alert=True)
            return

        current_val = sellers[0].settings.auto_join_promos if sellers[0].settings else False
        new_val = not current_val

        # Проверяем, заданы ли Стоп-цены при включении автовхода
        stop_cnt = (await session.execute(
            select(func.count(Product.id)).join(Seller).where(
                Seller.telegram_id == callback.from_user.id,
                Product.min_price > 0
            )
        )).scalar() or 0

        for s in sellers:
            if not s.settings:
                s.settings = SellerSettings(seller_id=s.id, auto_join_promos=new_val)
                session.add(s.settings)
            else:
                s.settings.auto_join_promos = new_val
        await session.commit()

    acc, spp, price, auto_join, sid = await _load(callback.from_user.id)
    try:
        await callback.message.edit_text(
            text=_text(spp, price, auto_join, acc),
            parse_mode="Markdown",
            reply_markup=_keyboard(spp, price, auto_join, sid, acc)
        )
    except Exception:
        pass

    if auto_join and stop_cnt == 0:
        await callback.answer(
            "🤖 Автовход включён (скидка ≤ 8%).\n"
            "💡 Совет: загрузите Стоп-цены в разделе «📊 Мои товары» для 100% защиты маржи!",
            show_alert=True
        )
    else:
        mode_name = "🤖 Автоматический (скидка ≤ 8%)" if auto_join else "👤 Ручной (по кнопке)"
        await callback.answer(f"Режим акций: {mode_name}")


@router.callback_query(F.data.in_({"toggle_spp", "toggle_price", "toggle_promos"}))
async def cb_toggle_notification(callback: CallbackQuery):
    field = callback.data.replace("toggle_", "")
    async with AsyncSessionFactory() as session:
        acc = (await session.execute(
            select(UserAccount).where(UserAccount.telegram_id == callback.from_user.id)
        )).scalars().first()
        if not acc:
            acc = UserAccount(telegram_id=callback.from_user.id)
            session.add(acc)
        if field == "spp":
            acc.notify_spp = not acc.notify_spp
        elif field == "price":
            acc.notify_price = not acc.notify_price
        elif field == "promos":
            acc.notify_promos = not acc.notify_promos
        await session.commit()

    acc, spp, price, auto_join, seller_id = await _load(callback.from_user.id)
    try:
        await callback.message.edit_text(
            _text(spp, price, auto_join, acc),
            parse_mode="Markdown",
            reply_markup=_keyboard(spp, price, auto_join, seller_id, acc)
        )
    except Exception:
        pass
    await callback.answer("🔔 Настройки уведомлений обновлены")