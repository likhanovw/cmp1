import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import get_settings
from db import (
    AsyncSessionFactory,
    init_db,
    get_or_create_user,
    get_user_by_telegram_id,
    get_balance,
    transfer,
    get_last_transactions,
    admin_adjust_balance,
    User,
    get_valid_payment_request,
    create_payment_request,
    mark_payment_request_used,
)
from qr_utils import generate_request_token, generate_qr_png


settings = get_settings()
bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)


@asynccontextmanager
async def session_scope() -> AsyncSession:
    session: AsyncSession = AsyncSessionFactory()
    try:
        yield session
    finally:
        await session.close()


class TransferStates(StatesGroup):
    waiting_for_recipient = State()
    waiting_for_amount = State()


class AdminAdjustStates(StatesGroup):
    waiting_for_target = State()
    waiting_for_amount = State()
    waiting_for_confirm = State()


class PayRequestStates(StatesGroup):
    waiting_for_amount = State()


class RegistrationStates(StatesGroup):
    waiting_for_contact = State()
    waiting_for_nickname = State()


def main_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💸 Перевести", callback_data="menu_transfer")
    kb.button(text="📥 Запросить средства", callback_data="menu_request")
    kb.button(text="📋 История", callback_data="menu_history")
    if is_admin:
        kb.button(text="⚙️ Админ-панель", callback_data="menu_admin")
    kb.adjust(1)
    return kb.as_markup()


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Начислить валюту", callback_data="admin_credit")
    kb.button(text="➖ Списать валюту", callback_data="admin_debit")
    kb.button(text="◀️ Назад", callback_data="admin_back")
    kb.adjust(1)
    return kb.as_markup()


def registration_inline_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Регистрация", callback_data="register_start")
    kb.adjust(1)
    return kb.as_markup()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает как обычный /start, так и /start <token> из deeplink/QR.
    """
    # Разбираем аргументы после /start
    args = (message.text or "").split(maxsplit=1)
    token = args[1].strip() if len(args) == 2 else ""

    await state.clear()
    async with session_scope() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        # обновим username, если пользователь уже есть
        if user and message.from_user.username is not None and user.username != message.from_user.username:
            user.username = message.from_user.username
            await session.commit()

    # Если пользователь не зарегистрирован — предлагаем регистрацию
    if not user or not user.is_registered:
        await message.answer(
            "👋 Чтобы пользоваться ботом, нужно сначала зарегистрироваться.\n\n"
            "Нажмите кнопку ниже, чтобы начать регистрацию.",
            reply_markup=registration_inline_keyboard(),
        )
        return

    # Если есть токен — это переход по QR/deeplink с запросом платежа
    if token:
        async with session_scope() as session:
            pr = await get_valid_payment_request(session, token)
            if not pr:
                await message.answer("❌ Этот запрос на перевод недействителен или истёк.")
                return

        await state.update_data(request_token=token)
        await state.set_state(PayRequestStates.waiting_for_amount)

        await message.answer(
            "Вы открыли запрос на получение средств.\n"
            "Введите сумму, которую хотите перевести получателю:",
        )
        return

    # Обычный старт без токена — показ главного меню и баланса
    async with session_scope() as session:
        # user здесь точно есть и зарегистрирован
        user = await get_user_by_telegram_id(session, message.from_user.id)
        balance = await get_balance(session, user)  # type: ignore[arg-type]

    title = "👑 Режим: Админ\n" if user.is_admin else ""

    await message.answer(
        f"💰 Баланс: <b>{balance:.2f} ₽</b>\n{title}",
        reply_markup=main_menu_keyboard(user.is_admin),
    )


@router.callback_query(F.data == "register_start")
async def on_register_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Поделиться", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await state.set_state(RegistrationStates.waiting_for_contact)
    await callback.message.answer(
        "Для регистрации поделитесь, пожалуйста, своим Telegram-контактом.\n"
        "Нажмите кнопку «Поделиться» ниже.",
        reply_markup=kb,
    )
    await callback.answer()


@router.message(RegistrationStates.waiting_for_contact)
async def on_register_contact(message: Message, state: FSMContext) -> None:
    contact = message.contact
    if contact is None:
        await message.answer(
            "❌ Мне не пришёл контакт.\n"
            "Пожалуйста, используйте кнопку «Поделиться» под полем ввода.",
        )
        return

    # Защита: принимаем только собственный контакт пользователя
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer(
            "❌ Нужно поделиться именно своим контактом.",
        )
        return

    username = message.from_user.username or contact.first_name or ""
    await state.update_data(username=username)
    await state.set_state(RegistrationStates.waiting_for_nickname)

    await message.answer(
        "Отлично! Теперь напишите ваш игровой ник.\n"
        "Это имя будут видеть другие пользователи.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(RegistrationStates.waiting_for_nickname)
async def on_register_nickname(message: Message, state: FSMContext) -> None:
    nickname = (message.text or "").strip()
    if not nickname:
        await message.answer("❌ Ник не может быть пустым. Введите, пожалуйста, ваш игровой ник.")
        return

    data = await state.get_data()
    username = data.get("username") or message.from_user.username

    async with session_scope() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if user:
            user.username = username
            user.game_nickname = nickname
            user.is_registered = True
            # если это супер-админ — не потеряем флаг
            if settings.super_admin_id == message.from_user.id:
                user.is_admin = True
            await session.commit()
        else:
            user = User(
                telegram_id=message.from_user.id,
                username=username,
                game_nickname=nickname,
                is_registered=True,
                is_admin=settings.super_admin_id == message.from_user.id,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

        balance = await get_balance(session, user)  # type: ignore[arg-type]

    await state.clear()
    title = "👑 Режим: Админ\n" if user.is_admin else ""
    await message.answer(
        "✅ Регистрация завершена! Теперь вы можете пользоваться ботом.",
    )
    await message.answer(
        f"💰 Баланс: <b>{balance:.2f} ₽</b>\n{title}",
        reply_markup=main_menu_keyboard(user.is_admin),
    )


@router.callback_query(F.data == "menu_back")
async def on_menu_back(callback: CallbackQuery, state: FSMContext) -> None:
    await cmd_start(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "menu_history")
async def on_menu_history(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
        )
        tx_list = await get_last_transactions(session, user)

    if not tx_list:
        text = "📋 История пуста."
    else:
        lines = ["📋 <b>Последние операции:</b>"]
        for tx in tx_list:
            sign = ""
            if tx.from_user_id == user.id:
                sign = "-"
            elif tx.to_user_id == user.id:
                sign = "+"
            lines.append(f"{sign}{float(tx.amount):.2f} ₽ • {tx.type}")
        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        reply_markup=main_menu_keyboard(is_admin=user.is_admin),
    )
    await callback.answer()


@router.callback_query(F.data == "menu_transfer")
async def on_menu_transfer(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TransferStates.waiting_for_recipient)
    await callback.message.edit_text(
        "Введите @username или ID получателя:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_back")]
            ]
        ),
    )
    await callback.answer()


async def resolve_user_by_text(session: AsyncSession, text: str) -> Optional[User]:
    # Попытка как ID
    text = text.strip()
    if text.startswith("@"):
        username = text[1:]
        result = await session.execute(
            select(User).where(User.username == username)
        )
        return result.scalar_one_or_none()
    if text.isdigit():
        tg_id = int(text)
        result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        return result.scalar_one_or_none()
    return None


@router.message(TransferStates.waiting_for_recipient)
async def on_transfer_recipient(message: Message, state: FSMContext) -> None:
    async with session_scope() as session:
        target = await resolve_user_by_text(session, message.text or "")

    if not target:
        await message.answer("❌ Пользователь не найден. Отправьте @username или ID ещё раз.")
        return

    await state.update_data(recipient_id=target.telegram_id)
    await state.set_state(TransferStates.waiting_for_amount)
    await message.answer("Введите сумму перевода в ₽ (например, 100.50):")


@router.message(TransferStates.waiting_for_amount)
async def on_transfer_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.replace(",", "."))
    except Exception:
        await message.answer("❌ Некорректная сумма. Попробуйте ещё раз.")
        return

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return

    data = await state.get_data()
    recipient_tg_id = data.get("recipient_id")
    async with session_scope() as session:
        sender = await get_or_create_user(
            session, telegram_id=message.from_user.id, username=message.from_user.username
        )
        result = await session.execute(
            select(User).where(User.telegram_id == recipient_tg_id)
        )
        recipient = result.scalar_one_or_none()
        if not recipient:
            await message.answer("❌ Получатель больше не существует.")
            await state.clear()
            return

        ok = await transfer(session, sender, recipient, amount)

    if not ok:
        balance = await get_balance(session, sender)  # type: ignore[name-defined]
        await message.answer(
            f"❌ Недостаточно средств. Ваш баланс: {balance:.2f} ₽"
        )
    else:
        await message.answer(
            f"✅ Перевод выполнен! С вашего счёта списано {amount:.2f} ₽"
        )
    await state.clear()


@router.callback_query(F.data == "menu_request")
async def on_menu_request(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
        )
        token = generate_request_token()
        pr = await create_payment_request(session, user, token)

    deep_link = f"https://t.me/{(await bot.me()).username}?start={token}"
    png_bytes = generate_qr_png(deep_link)

    photo = BufferedInputFile(png_bytes, filename="request.png")

    caption = (
        "📥 Покажите этот QR-код отправителю.\n"
        "Он откроет бота с предзаполненным получателем.\n"
        f"Срок действия: {settings.qr_expire_minutes} минут."
    )

    await callback.message.answer_photo(photo=photo, caption=caption)
    await callback.answer("QR-код с запросом средств создан.")


@router.message(PayRequestStates.waiting_for_amount)
async def on_pay_request_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.replace(",", "."))
    except Exception:
        await message.answer("❌ Некорректная сумма. Попробуйте ещё раз.")
        return

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return

    data = await state.get_data()
    token = data.get("request_token")
    if not token:
        await message.answer("❌ Запрос не найден. Начните с /start ещё раз.")
        await state.clear()
        return

    async with session_scope() as session:
        sender = await get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
        )
        pr = await get_valid_payment_request(session, token)
        if not pr:
            await message.answer("❌ Запрос больше недействителен.")
            await state.clear()
            return
        recipient = await session.get(User, pr.requester_id)
        if not recipient:
            await message.answer("❌ Получатель больше не существует.")
            await state.clear()
            return

        ok = await transfer(session, sender, recipient, amount)
        if ok:
            await mark_payment_request_used(session, pr)

    if not ok:
        async with session_scope() as session:
            sender = await get_or_create_user(
                session,
                telegram_id=message.from_user.id,
                username=message.from_user.username,
            )
            balance = await get_balance(session, sender)
        await message.answer(
            f"❌ Недостаточно средств. Ваш баланс: {balance:.2f} ₽"
        )
    else:
        await message.answer(
            f"✅ Перевод выполнен! С вашего счёта списано {amount:.2f} ₽"
        )

    await state.clear()


@router.callback_query(F.data == "menu_admin")
async def on_menu_admin(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
        )
    if not user.is_admin:
        await callback.answer("Нет прав доступа.", show_alert=True)
        return

    await callback.message.edit_text(
        "⚙️ Админ-панель", reply_markup=admin_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "admin_back")
async def on_admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    await cmd_start(callback.message, state)
    await callback.answer()


async def _admin_start_adjust(
    callback: CallbackQuery,
    state: FSMContext,
    is_credit: bool,
) -> None:
    async with session_scope() as session:
        admin = await get_or_create_user(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
        )
    if not admin.is_admin:
        await callback.answer("Нет прав доступа.", show_alert=True)
        return

    await state.set_state(AdminAdjustStates.waiting_for_target)
    await state.update_data(is_credit=is_credit)
    await callback.message.edit_text(
        "Введите @username или ID пользователя для изменения баланса:",
        reply_markup=admin_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_credit")
async def on_admin_credit(callback: CallbackQuery, state: FSMContext) -> None:
    await _admin_start_adjust(callback, state, is_credit=True)


@router.callback_query(F.data == "admin_debit")
async def on_admin_debit(callback: CallbackQuery, state: FSMContext) -> None:
    await _admin_start_adjust(callback, state, is_credit=False)


@router.message(AdminAdjustStates.waiting_for_target)
async def on_admin_target(message: Message, state: FSMContext) -> None:
    async with session_scope() as session:
        target = await resolve_user_by_text(session, message.text or "")

    if not target:
        await message.answer("❌ Пользователь не найден. Отправьте @username или ID ещё раз.")
        return

    await state.update_data(target_id=target.telegram_id)
    await state.set_state(AdminAdjustStates.waiting_for_amount)
    await message.answer("Введите сумму (положительное число):")


@router.message(AdminAdjustStates.waiting_for_amount)
async def on_admin_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.replace(",", "."))
    except Exception:
        await message.answer("❌ Некорректная сумма. Попробуйте ещё раз.")
        return

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return

    data = await state.get_data()
    is_credit = bool(data.get("is_credit"))
    sign = "+" if is_credit else "-"
    await state.update_data(amount=amount)
    await state.set_state(AdminAdjustStates.waiting_for_confirm)
    await message.answer(
        f"Подтвердите операцию:\n{sign}{amount:.2f} ₽\n\n"
        f"Отправьте 'ДА' для подтверждения или любое другое сообщение для отмены."
    )


@router.message(AdminAdjustStates.waiting_for_confirm)
async def on_admin_confirm(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    data = await state.get_data()

    if text != "да":
        await message.answer("Операция отменена.")
        await state.clear()
        return

    target_tg_id = data.get("target_id")
    is_credit = bool(data.get("is_credit"))
    amount = float(data.get("amount", 0))

    async with session_scope() as session:
        admin = await get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
        )
        if not admin.is_admin:
            await message.answer("Нет прав доступа.")
            await state.clear()
            return

        result = await session.execute(
            select(User).where(User.telegram_id == target_tg_id)
        )
        target = result.scalar_one_or_none()
        if not target:
            await message.answer("❌ Целевой пользователь больше не существует.")
            await state.clear()
            return

        await admin_adjust_balance(
            session,
            admin=admin,
            target=target,
            amount=amount,
            is_credit=is_credit,
        )

    if is_credit:
        await message.answer(f"💰 Администратор начислил {amount:.2f} ₽ пользователю.")
        try:
            await bot.send_message(
                chat_id=target_tg_id,
                text=f"💰 Администратор начислил вам {amount:.2f} ₽",
            )
        except Exception:
            pass
    else:
        await message.answer(f"⚠️ Администратор списал {amount:.2f} ₽ у пользователя.")
        try:
            await bot.send_message(
                chat_id=target_tg_id,
                text=f"⚠️ Администратор списал с вашего счёта {amount:.2f} ₽",
            )
        except Exception:
            pass

    await state.clear()


async def main() -> None:
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

