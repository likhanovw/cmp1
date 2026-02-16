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


# --- Админ-панель (временно отключена) ---
# class AdminAdjustStates(StatesGroup):
#     waiting_for_target = State()
#     waiting_for_amount = State()
#     waiting_for_confirm = State()


class PayRequestStates(StatesGroup):
    waiting_for_amount = State()   # плательщик вводит сумму (запрос "любой суммы")
    waiting_confirm = State()      # плательщик видит конкретную сумму, ждёт Отправить/Отменить


class RequestSpecificStates(StatesGroup):
    waiting_for_amount = State()   # запрашивающий вводит конкретную сумму


class RegistrationStates(StatesGroup):
    waiting_for_contact = State()
    waiting_for_nickname = State()
    waiting_for_cmap_id = State()


def main_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Запросить средства", callback_data="menu_request")
    # Админ-панель временно отключена
    # if is_admin:
    #     kb.button(text="⚙️ Админ-панель", callback_data="menu_admin")
    kb.adjust(1)
    return kb.as_markup()


def main_menu_text(user, balance: float) -> str:
    """Текст главного меню: игровое имя, игровой номер, баланс."""
    nickname = (user.game_nickname or "—") if user else "—"
    camp_id = (user.cmap_id or "—") if user else "—"
    return (
        f"👤 Игровое имя: <b>{nickname}</b>\n"
        f"🎯 Игровой номер: <b>{camp_id}</b>\n"
        f"💰 Баланс: <b>{balance:.2f} ₽</b>"
    )


# def admin_menu_keyboard() -> InlineKeyboardMarkup:
#     kb = InlineKeyboardBuilder()
#     kb.button(text="➕ Начислить валюту", callback_data="admin_credit")
#     kb.button(text="➖ Списать валюту", callback_data="admin_debit")
#     kb.button(text="◀️ Назад", callback_data="admin_back")
#     kb.adjust(1)
#     return kb.as_markup()


def registration_inline_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Регистрация", callback_data="register_start")
    kb.adjust(1)
    return kb.as_markup()


def request_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Запросить конкретную сумму", callback_data="request_specific")
    kb.button(text="Запросить любую сумму", callback_data="request_any")
    kb.button(text="◀️ Назад", callback_data="menu_back")
    kb.adjust(1)
    return kb.as_markup()


def pay_confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить", callback_data="pay_confirm")
    kb.button(text="Отменить", callback_data="pay_cancel")
    kb.adjust(1)
    return kb.as_markup()


def main_menu_button_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Главное меню", callback_data="menu_back")
    kb.adjust(1)
    return kb.as_markup()


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    *,
    telegram_id: Optional[int] = None,
    username: Optional[str] = None,
) -> None:
    """
    Обрабатывает как обычный /start, так и /start <token> из deeplink/QR.
    При вызове из callback (например «Назад») передать telegram_id и username,
    т.к. message.from_user там — бот, а не пользователь.
    """
    # Токен из deeplink только если сообщение — реальная команда /start (не текст отредактированного сообщения)
    raw = (message.text or "").strip()
    if raw.startswith("/start") and len(raw) > 6:
        args = raw.split(maxsplit=1)
        token = args[1].strip() if len(args) == 2 else ""
    else:
        token = ""

    uid = telegram_id if telegram_id is not None else message.from_user.id
    uname = username if username is not None else message.from_user.username

    await state.clear()
    async with session_scope() as session:
        user = await get_user_by_telegram_id(session, uid)
        # обновим username, если пользователь уже есть
        if user and uname is not None and user.username != uname:
            user.username = uname
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
            requester = await session.get(User, pr.requester_id)
            if not requester:
                await message.answer("❌ Запрос недействителен.")
                return

        # Запрос с конкретной суммой: показываем кто и сколько, кнопки Отправить/Отменить
        if pr.amount is not None:
            amount = float(pr.amount)
            name = requester.game_nickname or requester.username or f"ID{requester.telegram_id}"
            await state.update_data(request_token=token)
            await state.set_state(PayRequestStates.waiting_confirm)
            await message.answer(
                f"💸 <b>{name}</b> запрашивает у вас <b>{amount:.2f} ₽</b>.\n\n"
                "Отправьте или отмените:",
                reply_markup=pay_confirm_keyboard(),
            )
            return

        # Запрос без суммы: просим ввести сумму
        requester_name = requester.game_nickname or requester.username or f"ID{requester.telegram_id}"
        await state.update_data(request_token=token)
        await state.set_state(PayRequestStates.waiting_for_amount)
        await message.answer(
            f"💸 Запрос от <b>{requester_name}</b>.\n\n"
            "Введите сумму, которую хотите перевести получателю:",
        )
        return

    # Обычный старт без токена — показ главного меню и баланса
    async with session_scope() as session:
        # user здесь точно есть и зарегистрирован
        user = await get_user_by_telegram_id(session, uid)
        balance = await get_balance(session, user)  # type: ignore[arg-type]

    # title = "👑 Режим: Админ\n" if user.is_admin else ""

    await message.answer(
        main_menu_text(user, balance),
        reply_markup=main_menu_keyboard(is_admin=False),
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

    await state.update_data(nickname=nickname)
    await state.set_state(RegistrationStates.waiting_for_cmap_id)
    await message.answer(
        "Теперь укажите ваш игровой номер (cmap_id).\n"
        "Этот номер выдаётся в жизни и нужен для идентификации в игре.",
    )


@router.message(RegistrationStates.waiting_for_cmap_id)
async def on_register_cmap_id(message: Message, state: FSMContext) -> None:
    cmap_id = (message.text or "").strip()
    if not cmap_id:
        await message.answer("❌ Номер в игре не может быть пустым. Введите, пожалуйста, ваш игровой номер.")
        return

    data = await state.get_data()
    username = data.get("username") or message.from_user.username
    nickname = data.get("nickname")

    async with session_scope() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if user:
            user.username = username
            user.game_nickname = nickname
            user.cmap_id = cmap_id
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
                cmap_id=cmap_id,
                is_registered=True,
                is_admin=settings.super_admin_id == message.from_user.id,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

        balance = await get_balance(session, user)  # type: ignore[arg-type]

    await state.clear()
    await message.answer(
        "✅ Регистрация завершена! Теперь вы можете пользоваться ботом.",
    )
    await message.answer(
        main_menu_text(user, balance),
        reply_markup=main_menu_keyboard(is_admin=False),
    )


@router.callback_query(F.data == "menu_back")
async def on_menu_back(callback: CallbackQuery, state: FSMContext) -> None:
    await cmd_start(
        callback.message,
        state,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
    )
    await callback.answer()


@router.callback_query(F.data == "menu_request")
async def on_menu_request(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "📥 Запросить средства",
        reply_markup=request_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "request_any")
async def on_request_any(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
        )
        token = generate_request_token()
        await create_payment_request(session, user, token)

    deep_link = f"https://t.me/{(await bot.me()).username}?start={token}"
    png_bytes = generate_qr_png(deep_link)
    photo = BufferedInputFile(png_bytes, filename="request.png")
    caption = (
        "📥 Покажите этот QR-код отправителю.\n"
        "Он откроет бота и введёт сумму перевода.\n"
        f"Срок действия: {settings.qr_expire_minutes} мин."
    )
    await callback.message.answer_photo(photo=photo, caption=caption)
    await callback.answer("QR-код создан.")


@router.callback_query(F.data == "request_specific")
async def on_request_specific(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RequestSpecificStates.waiting_for_amount)
    await callback.message.edit_text(
        "Введите сумму, которую хотите запросить у другого игрока (в ₽):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_request")]
            ]
        ),
    )
    await callback.answer()


@router.message(RequestSpecificStates.waiting_for_amount)
async def on_request_specific_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float((message.text or "").replace(",", "."))
    except Exception:
        await message.answer("❌ Введите число, например: 100 или 50.5")
        return
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return

    async with session_scope() as session:
        user = await get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
        )
        token = generate_request_token()
        await create_payment_request(session, user, token, amount=amount)
        balance = await get_balance(session, user)  # type: ignore[arg-type]

    deep_link = f"https://t.me/{(await bot.me()).username}?start={token}"
    png_bytes = generate_qr_png(deep_link)
    photo = BufferedInputFile(png_bytes, filename="request.png")
    caption = (
        f"📥 Запрос <b>{amount:.2f} ₽</b>.\n"
        "Покажите этот QR-код отправителю — ему покажут сумму и кнопки «Отправить» / «Отменить».\n"
        f"Срок действия: {settings.qr_expire_minutes} мин."
    )
    await message.answer_photo(photo=photo, caption=caption)
    await state.clear()
    await message.answer(
        "Готово. Ожидайте перевода.\n\n" + main_menu_text(user, balance),
        reply_markup=main_menu_keyboard(is_admin=False),
    )


@router.callback_query(F.data == "pay_confirm")
async def on_pay_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    token = data.get("request_token")
    if not token:
        await callback.answer("Сессия истекла. Начните с /start.", show_alert=True)
        return

    async with session_scope() as session:
        pr = await get_valid_payment_request(session, token)
        if not pr or pr.amount is None:
            await callback.message.edit_text("❌ Запрос недействителен или истёк.")
            await state.clear()
            await callback.answer()
            return
        sender = await get_or_create_user(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
        )
        recipient = await session.get(User, pr.requester_id)
        if not recipient:
            await callback.message.edit_text("❌ Получатель не найден.")
            await state.clear()
            await callback.answer()
            return
        amount = float(pr.amount)
        ok = await transfer(session, sender, recipient, amount)
        if ok:
            await mark_payment_request_used(session, pr)
        recipient_tg_id = recipient.telegram_id
        sender_name = sender.game_nickname or sender.username or f"ID{sender.telegram_id}"

    await state.clear()
    if not ok:
        async with session_scope() as session:
            sender = await get_or_create_user(
                session,
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
            )
            balance = await get_balance(session, sender)
        await callback.message.edit_text(
            f"❌ Недостаточно средств. Ваш баланс: {balance:.2f} ₽"
        )
        await callback.answer()
        return

    async with session_scope() as session:
        sender = await get_user_by_telegram_id(session, callback.from_user.id)
        balance = await get_balance(session, sender) if sender else 0
    await callback.message.edit_text(f"✅ Переведено {amount:.2f} ₽ получателю.")
    await callback.answer()
    await callback.message.answer(
        main_menu_text(sender, balance),
        reply_markup=main_menu_keyboard(is_admin=False),
    )

    # Уведомление получателю (кто запрашивал)
    async with session_scope() as session:
        rec = await get_user_by_telegram_id(session, recipient_tg_id)
        rec_balance = await get_balance(session, rec) if rec else 0
    try:
        await bot.send_message(
            chat_id=recipient_tg_id,
            text=(
                f"💸 <b>Перевод по вашему запросу</b>\n\n"
                f"Перевод от: <b>{sender_name}</b>\n"
                f"Сумма: <b>{amount:.2f} ₽</b>\n"
                f"Общий баланс: <b>{rec_balance:.2f} ₽</b>"
            ),
            reply_markup=main_menu_button_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "pay_cancel")
async def on_pay_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cmd_start(
        callback.message,
        state,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
    )
    await callback.answer()


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
        recipient_tg_id = recipient.telegram_id
        sender_name = sender.game_nickname or sender.username or f"ID{sender.telegram_id}"
        recipient_name = recipient.game_nickname or recipient.username or f"ID{recipient.telegram_id}"
        sender_balance = await get_balance(session, sender) if ok else 0

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
            "✅ Перевод выполнен!\n\n"
            f"Кому: <b>{recipient_name}</b>\n"
            f"Сколько: <b>{amount:.2f} ₽</b>\n"
            f"Текущий баланс: <b>{sender_balance:.2f} ₽</b>"
        )
        # Уведомление получателю (кто запрашивал)
        async with session_scope() as session:
            rec = await get_user_by_telegram_id(session, recipient_tg_id)
            rec_balance = await get_balance(session, rec) if rec else 0
        try:
            await bot.send_message(
                chat_id=recipient_tg_id,
                text=(
                    f"💸 <b>Перевод по вашему запросу</b>\n\n"
                    f"Перевод от: <b>{sender_name}</b>\n"
                    f"Сумма: <b>{amount:.2f} ₽</b>\n"
                    f"Общий баланс: <b>{rec_balance:.2f} ₽</b>"
                ),
                reply_markup=main_menu_button_keyboard(),
            )
        except Exception:
            pass

    await state.clear()


@router.callback_query(F.data == "menu_admin")
async def on_menu_admin(callback: CallbackQuery) -> None:
    await callback.answer("Админ-панель временно недоступна.", show_alert=True)



async def main() -> None:
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

