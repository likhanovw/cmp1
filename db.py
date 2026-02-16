from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import (
    Integer,
    String,
    DateTime,
    BigInteger,
    ForeignKey,
    Boolean,
    Numeric,
    select,
    func,
    text,
)
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
    AsyncSession,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from config import get_settings


settings = get_settings()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    game_nickname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Храним в колонке camp_id, но в коде используем имя cmap_id
    cmap_id: Mapped[Optional[str]] = mapped_column("camp_id", String(64), nullable=True)
    is_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # мягкое удаление админом
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    balance: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def is_active(self) -> bool:
        """Зарегистрирован и не удалён — может пользоваться ботом и попадать в списки."""
        return bool(self.is_registered and not self.is_deleted)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    from_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    to_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    amount: Mapped[float] = mapped_column(Numeric(18, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    type: Mapped[str] = mapped_column(String(50))  # transfer, admin_credit, admin_debit
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    from_user: Mapped[Optional[User]] = relationship("User", foreign_keys=[from_user_id])
    to_user: Mapped[Optional[User]] = relationship("User", foreign_keys=[to_user_id])


class PaymentRequest(Base):
    __tablename__ = "payment_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    requester_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    amount: Mapped[Optional[float]] = mapped_column(Numeric(18, 2), nullable=True)  # None = любая сумма
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)

    requester: Mapped[User] = relationship("User")


engine = create_async_engine(settings.db_url, echo=False, future=True)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)


def _add_is_deleted_column_if_missing(sync_conn) -> None:
    """Миграция: добавить колонку is_deleted в users, если её ещё нет."""
    cursor = sync_conn.execute(text("PRAGMA table_info(users)"))
    rows = cursor.fetchall()
    col_names = [row[1] for row in rows] if rows else []
    if "is_deleted" not in col_names:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN is_deleted BOOLEAN DEFAULT 0"))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_is_deleted_column_if_missing)


async def get_user_by_telegram_id(
    session: AsyncSession,
    telegram_id: int,
) -> Optional[User]:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def get_user_by_camp_id(
    session: AsyncSession,
    camp_id: str,
) -> Optional[User]:
    result = await session.execute(
        select(User).where(User.cmap_id == camp_id.strip())
    )
    return result.scalar_one_or_none()


async def get_user_by_game_nickname(
    session: AsyncSession,
    game_nickname: str,
    only_active: bool = True,
) -> Optional[User]:
    q = select(User).where(User.game_nickname == game_nickname.strip())
    if only_active:
        q = q.where(User.is_registered.is_(True), User.is_deleted.is_(False))
    result = await session.execute(q)
    return result.scalar_one_or_none()


async def get_all_registered_players(
    session: AsyncSession,
    limit: int = 100,
) -> list[User]:
    """Список активных игроков (зарегистрированы и не удалены) для админ-панели."""
    result = await session.execute(
        select(User)
        .where(User.is_registered.is_(True), User.is_deleted.is_(False))
        .order_by(User.game_nickname)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: Optional[str],
) -> User:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user:
        # обновим username, если поменялся
        if username is not None and user.username != username:
            user.username = username
            await session.commit()
        return user

    user = User(
        telegram_id=telegram_id,
        username=username,
        is_admin=settings.super_admin_id == telegram_id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def create_payment_request(
    session: AsyncSession,
    requester: User,
    token: str,
    amount: Optional[float] = None,
) -> PaymentRequest:
    now = datetime.utcnow()
    expires_at = now + timedelta(minutes=settings.qr_expire_minutes)
    pr = PaymentRequest(
        token=token,
        requester_id=requester.id,
        amount=amount,
        created_at=now,
        expires_at=expires_at,
        used=False,
    )
    session.add(pr)
    await session.commit()
    await session.refresh(pr)
    return pr


async def get_valid_payment_request(
    session: AsyncSession,
    token: str,
) -> Optional[PaymentRequest]:
    now = datetime.utcnow()
    result = await session.execute(
        select(PaymentRequest)
        .where(PaymentRequest.token == token)
        .where(PaymentRequest.expires_at >= now)
        .where(PaymentRequest.used.is_(False))
    )
    return result.scalar_one_or_none()


async def mark_payment_request_used(
    session: AsyncSession,
    pr: PaymentRequest,
) -> None:
    pr.used = True
    await session.commit()


async def get_balance(
    session: AsyncSession,
    user: User,
) -> float:
    result = await session.execute(
        select(User.balance).where(User.id == user.id)
    )
    balance = result.scalar_one()
    return float(balance or 0)


async def transfer(
    session: AsyncSession,
    from_user: User,
    to_user: User,
    amount: float,
    tx_type: str = "transfer",
    description: str | None = None,
) -> bool:
    await session.refresh(from_user)
    await session.refresh(to_user)

    if from_user.balance < amount:
        return False

    from_user.balance = float(from_user.balance) - amount
    to_user.balance = float(to_user.balance) + amount

    tx = Transaction(
        from_user_id=from_user.id,
        to_user_id=to_user.id,
        amount=amount,
        type=tx_type,
        description=description,
    )
    session.add(tx)
    await session.commit()
    return True


async def admin_adjust_balance(
    session: AsyncSession,
    admin: User,
    target: User,
    amount: float,
    is_credit: bool,
    description: str | None = None,
) -> None:
    await session.refresh(target)
    if is_credit:
        target.balance = float(target.balance) + amount
        tx_type = "admin_credit"
    else:
        target.balance = float(target.balance) - amount
        tx_type = "admin_debit"

    tx = Transaction(
        from_user_id=None,
        to_user_id=target.id,
        amount=amount,
        type=tx_type,
        description=description or f"admin:{admin.telegram_id}",
    )
    session.add(tx)
    await session.commit()


async def get_last_transactions(
    session: AsyncSession,
    user: User,
    limit: int = 20,
) -> list[Transaction]:
    result = await session.execute(
        select(Transaction)
        .options(
            selectinload(Transaction.from_user),
            selectinload(Transaction.to_user),
        )
        .where(
            (Transaction.from_user_id == user.id)
            | (Transaction.to_user_id == user.id)
        )
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())

