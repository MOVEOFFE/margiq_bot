import asyncio
import io
import csv
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func

from database import AsyncSessionFactory
from models import UserAccount, Seller, Product, PromoCode
from config import settings
from utils import csv_safe

router = Router()


class AdminStates(StatesGroup):
    waiting_for_sub_grant = State()      # Формат: TG_ID ДНИ МАКС_МАГАЗИНОВ
    waiting_for_broadcast_msg = State()  # Сообщение для рассылки
    waiting_for_ban_id = State()         # TG_ID для бана/разбана
    waiting_for_promo_data = State()     # Формат: КОД СКИДКА% ЛИМИТ ДНЕЙ


def is_user_admin(user_id: int) -> bool:
    """Мгновенная синхронная проверка прав администратора по ADMIN_IDS из .env."""
    return user_id in settings.admin_ids_list


def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Выдать / Изменить тариф", callback_data="adm_grant_sub")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="📥 Выгрузить базу селлеров (CSV)", callback_data="adm_export_users")],
        [InlineKeyboardButton(text="🚫 Бан / Разбан по ID", callback_data="adm_ban_user")],
        [InlineKeyboardButton(text="🏷 Промокоды", callback_data="adm_promo_codes")],
        [InlineKeyboardButton(text="🔄 Обновить статистику", callback_data="adm_refresh")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main_menu")]
    ])


async def render_admin_dashboard() -> str:
    now = datetime.utcnow()
    async with AsyncSessionFactory() as session:
        total_accounts = (await session.execute(select(func.count(UserAccount.id)))).scalar() or 0
        active_subs = (await session.execute(
            select(func.count(UserAccount.id)).where(
                UserAccount.subscription_until > now,
                UserAccount.is_banned == False
            )
        )).scalar() or 0
        total_shops = (await session.execute(select(func.count(Seller.id)))).scalar() or 0
        wb_shops = (await session.execute(
            select(func.count(Seller.id)).where(Seller.marketplace == "wb", Seller.wb_token_encrypted.is_not(None))
        )).scalar() or 0
        ozon_shops = (await session.execute(
            select(func.count(Seller.id)).where(Seller.marketplace == "ozon", Seller.ozon_api_key_encrypted.is_not(None))
        )).scalar() or 0
        total_goods = (await session.execute(select(func.count(Product.id)))).scalar() or 0

    return (
        "👑 **Панель управления Margiq (Admin SaaS)**\n\n"
        "📊 **Статистика проекта:**\n"
        f"• 👥 Всего зарегистрировано: **{total_accounts} селлеров**\n"
        f"• 💎 С активной подпиской: **{active_subs}**\n"
        f"• 🏪 Подключено магазинов: **{total_shops}** (🟣 WB: `{wb_shops}` | 🔵 Ozon: `{ozon_shops}`)\n"
        f"• 📦 Товаров под мониторингом: **{total_goods} SKU**\n\n"
        "Выберите действие ниже:"
    )


@router.message(Command("admin"))
@router.message(F.text == "👑 Админ-панель")
async def cmd_admin(message: Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return
    await state.clear()
    text = await render_admin_dashboard()
    await message.answer(text, parse_mode="Markdown", reply_markup=get_admin_keyboard())


@router.callback_query(F.data == "adm_refresh")
async def cb_adm_refresh(callback: CallbackQuery, state: FSMContext):
    if not is_user_admin(callback.from_user.id):
        return
    await state.clear()
    text = await render_admin_dashboard()
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=get_admin_keyboard())
    except Exception:
        pass
    await callback.answer("Статистика обновлена")


@router.callback_query(F.data == "adm_grant_sub")
async def cb_adm_grant_sub(callback: CallbackQuery, state: FSMContext):
    if not is_user_admin(callback.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_refresh")]])
    await callback.message.edit_text(
        "🎁 **Выдача подписки и лимита магазинов**\n\n"
        "Отправьте данные через пробел в формате:\n"
        "`TELEGRAM_ID ДНИ ЛИМИТ_МАГАЗИНОВ`\n\n"
        "📌 *Примеры:*\n"
        "• `590568180 30 3` — подписка на 30 дней, лимит до 3 магазинов\n"
        "• `590568180 365 99` — годовой безлимит (99 магазинов)\n"
        "• `590568180 0 1` — обнулить подписку",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await state.set_state(AdminStates.waiting_for_sub_grant)
    await callback.answer()


@router.message(AdminStates.waiting_for_sub_grant)
async def process_sub_grant(message: Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return

    parts = (message.text or "").strip().split()
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        await message.answer("⚠️ Неверный формат. Отправьте 3 числа через пробел: `TELEGRAM_ID ДНИ ЛИМИТ_МАГАЗИНОВ`", parse_mode="Markdown")
        return

    target_id = int(parts[0])
    days = int(parts[1])
    max_shops = int(parts[2])
    new_until = datetime.utcnow() + timedelta(days=days)

    async with AsyncSessionFactory() as session:
        acc = (await session.execute(
            select(UserAccount).where(UserAccount.telegram_id == target_id)
        )).scalars().first()

        if not acc:
            acc = UserAccount(
                telegram_id=target_id,
                max_shops=max_shops,
                subscription_until=new_until,
                tariff="manual",
                is_banned=False
            )
            session.add(acc)
        else:
            acc.max_shops = max_shops
            acc.subscription_until = new_until
            acc.tariff = "manual"
            acc.is_banned = False

        await session.commit()

    await state.clear()
    until_str = new_until.strftime("%d.%m.%Y")

    try:
        if days > 0:
            await message.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🎉 **Ваш тариф в Margiq успешно активирован!**\n\n"
                    f"• 📅 Подписка активна до: **{until_str}** (`{days} дн.`)\n"
                    f"• 🏪 Доступный лимит магазинов: **до {max_shops} шт.**\n\n"
                    f"Спасибо, что пользуетесь сервисом!"
                ),
                parse_mode="Markdown"
            )
    except Exception:
        pass

    await message.answer(
        f"✅ Тариф для пользователя `{target_id}` обновлен!\n"
        f"• Срок: до **{until_str}**\n"
        f"• Лимит магазинов: **{max_shops}**",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard()
    )


@router.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_user_admin(callback.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_refresh")]])
    await callback.message.edit_text(
        "📢 **Массовая рассылка по всем селлерам**\n\n"
        "Отправьте сообщение (текст, фото с подписью, видео или документ), которое нужно разослать всем пользователям бота:",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await state.set_state(AdminStates.waiting_for_broadcast_msg)
    await callback.answer()


@router.message(AdminStates.waiting_for_broadcast_msg)
async def process_broadcast(message: Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return

    await state.clear()
    status_msg = await message.answer("⏳ Запускаем рассылку...")

    async with AsyncSessionFactory() as session:
        acc_ids = (await session.execute(select(UserAccount.telegram_id))).scalars().all()
        seller_ids = (await session.execute(select(func.distinct(Seller.telegram_id)))).scalars().all()
        all_target_ids = set(acc_ids) | set(seller_ids)

    sent_ok = 0
    sent_err = 0
    for uid in all_target_ids:
        try:
            await message.copy_to(chat_id=uid)
            sent_ok += 1
        except Exception:
            sent_err += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ **Рассылка завершена!**\n\n"
        f"• 📬 Доставлено: **{sent_ok}**\n"
        f"• ❌ Не доставлено (заблокировали бота): **{sent_err}**",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard()
    )


@router.callback_query(F.data == "adm_export_users")
async def cb_adm_export_users(callback: CallbackQuery):
    if not is_user_admin(callback.from_user.id):
        return
    await callback.answer("Формируем выгрузку пользователей...")

    async with AsyncSessionFactory() as session:
        accounts = (await session.execute(select(UserAccount))).scalars().all()
        sellers = (await session.execute(select(Seller))).scalars().all()

        shops_by_user = {}
        for s in sellers:
            if s.wb_token_encrypted or s.ozon_api_key_encrypted:
                shops_by_user[s.telegram_id] = shops_by_user.get(s.telegram_id, 0) + 1

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "Telegram ID",
            "Username",
            "Имя",
            "Тариф",
            "Подключено магазинов",
            "Лимит магазинов по тарифу",
            "Подписка до (UTC)",
            "Статус",
            "Дата регистрации"
        ])

        now = datetime.utcnow()
        for acc in accounts:
            is_active = acc.subscription_until and acc.subscription_until > now and not acc.is_banned
            status_str = "Заблокирован" if acc.is_banned else ("Активен" if is_active else "Истекла подписка")
            writer.writerow([
                f'="{acc.telegram_id}"',
                csv_safe(f"@{acc.username}" if acc.username else "—"),
                csv_safe(acc.full_name or "—"),
                acc.tariff or "trial",
                shops_by_user.get(acc.telegram_id, 0),
                acc.max_shops,
                acc.subscription_until.strftime("%d.%m.%Y %H:%M") if acc.subscription_until else "—",
                status_str,
                acc.created_at.strftime("%d.%m.%Y") if acc.created_at else "—"
            ])

    csv_bytes = output.getvalue().encode("utf-8")
    doc = BufferedInputFile(csv_bytes, filename=f"margiq_users_{datetime.now().strftime('%Y-%m-%d')}.csv")
    await callback.message.answer_document(document=doc, caption="📥 **База пользователей и подписок Margiq**", parse_mode="Markdown")


@router.callback_query(F.data == "adm_ban_user")
async def cb_adm_ban_user(callback: CallbackQuery, state: FSMContext):
    if not is_user_admin(callback.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_refresh")]])
    await callback.message.edit_text(
        "🚫 **Блокировка / Разблокировка пользователя**\n\n"
        "Отправьте `TELEGRAM_ID` пользователя (повторная отправка снимает бан):",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await state.set_state(AdminStates.waiting_for_ban_id)
    await callback.answer()


@router.message(AdminStates.waiting_for_ban_id)
async def process_ban_user(message: Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return

    if not message.text or not message.text.strip().isdigit():
        await message.answer("⚠️ Отправьте числовой `TELEGRAM_ID`.")
        return

    target_id = int(message.text.strip())
    async with AsyncSessionFactory() as session:
        acc = (await session.execute(select(UserAccount).where(UserAccount.telegram_id == target_id))).scalars().first()
        if not acc:
            acc = UserAccount(telegram_id=target_id, is_banned=True)
            session.add(acc)
            new_state = True
        else:
            acc.is_banned = not acc.is_banned
            new_state = acc.is_banned
        await session.commit()

    await state.clear()
    status_txt = "🚫 **заблокирован**" if new_state else "✅ **разблокирован**"
    await message.answer(f"Пользователь `{target_id}` {status_txt}.", parse_mode="Markdown", reply_markup=get_admin_keyboard())


@router.callback_query(F.data == "adm_promo_codes")
async def cb_adm_promo_codes(callback: CallbackQuery, state: FSMContext):
    if not is_user_admin(callback.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="adm_promo_create")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="adm_promo_list")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="adm_refresh")],
    ])
    await callback.message.edit_text("🏷 **Управление промокодами**\nВыберите действие:", parse_mode="Markdown", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm_promo_create")
async def cb_adm_promo_create(callback: CallbackQuery, state: FSMContext):
    if not is_user_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🏷 **Создание промокода**\n\n"
        "Отправьте через пробел:\n"
        "`КОД СКИДКА% ЛИМИТ ДНЕЙ`\n\n"
        "• КОД — например `WELCOME20`\n"
        "• СКИДКА% — от 1 до 100 (100% активирует тариф бесплатно)\n"
        "• ЛИМИТ — сколько раз можно использовать\n"
        "• ДНЕЙ — срок действия (дней от сегодня)\n\n"
        "Пример: `WELCOME20 20 100 30`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_promo_codes")]])
    )
    await state.set_state(AdminStates.waiting_for_promo_data)
    await callback.answer()


@router.message(AdminStates.waiting_for_promo_data)
async def process_promo_data(message: Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 4:
        await message.answer("⚠️ Нужно 4 значения: `КОД СКИДКА% ЛИМИТ ДНЕЙ`", parse_mode="Markdown")
        return
    code, disc_s, limit_s, days_s = parts
    if not disc_s.isdigit() or not limit_s.isdigit() or not days_s.isdigit():
        await message.answer("⚠️ Скидка, лимит и дни должны быть числами.")
        return
    discount = int(disc_s)
    limit = int(limit_s)
    days = int(days_s)
    if not (0 < discount <= 100):
        await message.answer("⚠️ Скидка должна быть от 1 до 100.")
        return
    code_upper = code.strip().upper()
    expires_at = datetime.utcnow() + timedelta(days=days)

    async with AsyncSessionFactory() as session:
        exists = (await session.execute(select(PromoCode).where(PromoCode.code == code_upper))).scalars().first()
        if exists:
            await message.answer(f"⚠️ Промокод `{code_upper}` уже существует.", parse_mode="Markdown")
            await state.clear()
            return
        session.add(PromoCode(code=code_upper, discount_percent=discount, max_uses=limit, used_count=0, expires_at=expires_at, active=True))
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Промокод `{code_upper}` создан:\n• Скидка: **{discount}%**\n• Лимит: **{limit}**\n• Действует до: {expires_at.strftime('%d.%m.%Y')}",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard()
    )


@router.callback_query(F.data == "adm_promo_list")
async def cb_adm_promo_list(callback: CallbackQuery):
    if not is_user_admin(callback.from_user.id):
        return
    async with AsyncSessionFactory() as session:
        promos = (await session.execute(select(PromoCode).order_by(PromoCode.id.desc()))).scalars().all()
    if not promos:
        await callback.message.edit_text(
            "🏷 Промокодов пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_promo_codes")]])
        )
        await callback.answer()
        return
    lines = ["🏷 **Активные и архивные промокоды:**\n"]
    for p in promos:
        ok = p.active and (not p.expires_at or p.expires_at > datetime.utcnow()) and p.used_count < p.max_uses
        exp_str = p.expires_at.strftime("%d.%m") if p.expires_at else "∞"
        lines.append(f"{'🟢' if ok else '🔴'} `{p.code}` — **-{p.discount_percent}%** · `{p.used_count}/{p.max_uses}` исп. (до {exp_str})")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm_promo_create")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_promo_codes")],
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)
    await callback.answer()