from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import YooMoneyPayment


logger = structlog.get_logger(__name__)


async def create_yoomoney_payment(
    db: AsyncSession,
    user_id: int | None,
    label: str,
    amount_kopeks: int,
    description: str,
    metadata_json: dict | None = None,
) -> YooMoneyPayment | None:
    existing = await get_yoomoney_payment_by_label(db, label)
    if existing is not None:
        return existing

    payment = YooMoneyPayment(
        user_id=user_id,
        label=label,
        amount_kopeks=amount_kopeks,
        description=description,
        status='pending',
        metadata_json=metadata_json,
    )

    db.add(payment)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        existing = await get_yoomoney_payment_by_label(db, label)
        if existing is not None:
            return existing
        logger.error(
            'IntegrityError при создании платежа ЮMoney',
            label=label,
            user_id=user_id,
            error=str(e),
        )
        return None
    await db.refresh(payment)

    logger.info(
        'Создан платеж ЮMoney',
        label=label,
        amount_rubles=amount_kopeks / 100,
        user_id=user_id,
    )
    return payment


async def get_yoomoney_payment_by_label(db: AsyncSession, label: str) -> YooMoneyPayment | None:
    result = await db.execute(
        select(YooMoneyPayment)
        .options(selectinload(YooMoneyPayment.user))
        .where(YooMoneyPayment.label == label)
    )
    return result.scalar_one_or_none()


async def get_yoomoney_payment_by_local_id(db: AsyncSession, local_id: int) -> YooMoneyPayment | None:
    result = await db.execute(
        select(YooMoneyPayment)
        .options(selectinload(YooMoneyPayment.user))
        .where(YooMoneyPayment.id == local_id)
    )
    return result.scalar_one_or_none()


async def update_yoomoney_payment_status(
    db: AsyncSession,
    label: str,
    status: str,
    operation_id: str | None = None,
    sender: str | None = None,
) -> YooMoneyPayment | None:
    update_data: dict = {'status': status, 'updated_at': datetime.now(UTC)}
    if operation_id:
        update_data['operation_id'] = operation_id
    if sender:
        update_data['sender'] = sender

    await db.execute(
        update(YooMoneyPayment).where(YooMoneyPayment.label == label).values(**update_data)
    )
    await db.commit()

    result = await db.execute(
        select(YooMoneyPayment)
        .options(selectinload(YooMoneyPayment.user))
        .where(YooMoneyPayment.label == label)
    )
    payment = result.scalar_one_or_none()

    if payment:
        logger.info(
            'Обновлён статус платежа ЮMoney',
            label=label,
            status=status,
        )

    return payment


async def link_yoomoney_payment_to_transaction(
    db: AsyncSession, label: str, transaction_id: int
) -> YooMoneyPayment | None:
    await db.execute(
        update(YooMoneyPayment)
        .where(YooMoneyPayment.label == label)
        .values(transaction_id=transaction_id, updated_at=datetime.now(UTC))
    )
    await db.flush()

    result = await db.execute(
        select(YooMoneyPayment)
        .options(selectinload(YooMoneyPayment.user), selectinload(YooMoneyPayment.transaction))
        .where(YooMoneyPayment.label == label)
    )
    payment = result.scalar_one_or_none()

    if payment:
        logger.info(
            'Платеж ЮMoney связан с транзакцией',
            label=label,
            transaction_id=transaction_id,
        )

    return payment


async def get_pending_yoomoney_payments(
    db: AsyncSession, user_id: int | None = None, limit: int = 100
) -> list[YooMoneyPayment]:
    query = select(YooMoneyPayment).options(selectinload(YooMoneyPayment.user))
    conditions = [YooMoneyPayment.status == 'pending']
    if user_id:
        conditions.append(YooMoneyPayment.user_id == user_id)
    result = await db.execute(
        query.where(and_(*conditions)).order_by(YooMoneyPayment.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
