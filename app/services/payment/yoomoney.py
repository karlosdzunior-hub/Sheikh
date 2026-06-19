"""Mixin для работы с ЮMoney (P2P-платежи через личный кошелёк)."""
from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.utils.payment_logger import payment_logger as logger


if TYPE_CHECKING:
    from app.database.models import YooMoneyPayment


def _make_label(user_id: int | None) -> str:
    """Генерирует уникальный label для платежа."""
    uid = user_id or 0
    ts = int(time.time())
    rand = secrets.token_hex(4)
    return f'bot_{uid}_{ts}_{rand}'


def _build_quickpay_url(
    wallet: str,
    amount_rubles: float,
    label: str,
    description: str,
    payment_type: str = 'AC',
) -> str:
    """Строит ссылку ЮMoney QuickPay."""
    params = {
        'receiver': wallet,
        'quickpay-form': 'button',
        'targets': description[:200],
        'sum': f'{amount_rubles:.2f}',
        'paymentType': payment_type,
        'label': label,
    }
    return 'https://yoomoney.ru/quickpay/confirm.xml?' + urlencode(params)


class YooMoneyPaymentMixin:
    """Mixin с операциями для ЮMoney P2P-платежей."""

    async def create_yoomoney_payment(
        self,
        db: AsyncSession,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт запись платежа ЮMoney и возвращает ссылку для оплаты."""
        from app.database.crud.yoomoney import create_yoomoney_payment

        wallet = settings.YOOMONEY_WALLET
        if not wallet:
            logger.error('YOOMONEY_WALLET не настроен')
            return None

        label = _make_label(user_id)
        amount_rubles = amount_kopeks / 100
        payment_type = getattr(settings, 'YOOMONEY_PAYMENT_TYPE', 'AC')

        payment_url = _build_quickpay_url(wallet, amount_rubles, label, description, payment_type)

        payment_metadata = dict(metadata or {})
        payment_metadata.update({
            'user_id': str(user_id) if user_id is not None else '',
            'amount_kopeks': str(amount_kopeks),
            'type': 'balance_topup',
        })

        local_payment = await create_yoomoney_payment(
            db=db,
            user_id=user_id,
            label=label,
            amount_kopeks=amount_kopeks,
            description=description,
            metadata_json=payment_metadata,
        )

        if not local_payment:
            logger.error('Не удалось создать запись платежа ЮMoney', user_id=user_id)
            return None

        logger.info(
            'Создан платеж ЮMoney',
            label=label,
            amount_rubles=amount_rubles,
            user_id=user_id,
        )

        return {
            'local_payment_id': local_payment.id,
            'label': label,
            'payment_url': payment_url,
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_rubles,
            'status': 'pending',
        }

    async def verify_yoomoney_payment(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API ЮMoney."""
        from app.database.crud.yoomoney import get_yoomoney_payment_by_local_id
        from app.external.yoomoney_webhook import check_yoomoney_payment_by_label

        payment = await get_yoomoney_payment_by_local_id(db, local_payment_id)
        if not payment:
            return None

        if payment.status == 'succeeded' and getattr(payment, 'transaction_id', None):
            return {'payment': payment, 'status': 'succeeded', 'already_processed': True}

        token = settings.YOOMONEY_TOKEN
        if not token:
            logger.warning('YOOMONEY_TOKEN не настроен — проверка через API недоступна')
            return {'payment': payment, 'status': payment.status, 'already_processed': False}

        operation = await check_yoomoney_payment_by_label(payment.label, token)

        if operation and operation.get('status') == 'success':
            await self._process_successful_yoomoney_payment(db, payment, operation)
            await db.refresh(payment)
            return {'payment': payment, 'status': 'succeeded', 'already_processed': False}

        return {'payment': payment, 'status': payment.status, 'already_processed': False}

    async def _process_successful_yoomoney_payment(
        self,
        db: AsyncSession,
        payment: YooMoneyPayment,
        operation: dict[str, Any] | None = None,
    ) -> bool:
        """Начисляет баланс пользователю после успешного платежа ЮMoney."""
        from sqlalchemy import select

        from app.database.models import PaymentMethod, YooMoneyPayment as YMPayment

        locked_result = await db.execute(
            select(YMPayment).where(YMPayment.id == payment.id).with_for_update()
        )
        payment = locked_result.scalar_one()

        if getattr(payment, 'transaction_id', None):
            logger.info(
                'Платёж ЮMoney уже обработан',
                label=payment.label,
                transaction_id=payment.transaction_id,
            )
            return True

        from app.database.crud.user import add_user_balance, get_user_by_id
        from app.database.crud.yoomoney import link_yoomoney_payment_to_transaction, update_yoomoney_payment_status
        from app.database.models import TransactionType

        operation_id = (operation or {}).get('operation_id') or ''
        sender = (operation or {}).get('sender') or ''

        await update_yoomoney_payment_status(
            db, payment.label, 'succeeded',
            operation_id=operation_id, sender=sender,
        )

        user = None
        if payment.user_id:
            user = await get_user_by_id(db, payment.user_id)

        if not user:
            logger.error('Пользователь не найден для платежа ЮMoney', label=payment.label)
            return False

        success = await add_user_balance(
            db=db,
            user=user,
            amount_kopeks=payment.amount_kopeks,
            description=f'Пополнение через {settings.YOOMONEY_DISPLAY_NAME}',
            create_transaction=True,
            transaction_type=TransactionType.DEPOSIT,
            bot=getattr(self, 'bot', None),
            payment_method=PaymentMethod.YOOMONEY,
            commit=False,
        )

        if not success:
            logger.error('add_user_balance вернул False для ЮMoney', label=payment.label)
            return False

        await db.flush()

        from app.database.crud.transaction import get_user_transactions

        transactions = await get_user_transactions(db, user.id, limit=1)
        if transactions:
            await link_yoomoney_payment_to_transaction(db, payment.label, transactions[0].id)

        await db.commit()

        logger.info(
            'ЮMoney платёж обработан — баланс пополнен',
            label=payment.label,
            user_id=user.id,
            amount_rubles=payment.amount_kopeks / 100,
        )

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                from app.localization.texts import get_texts
                texts = get_texts(user.language)
                await self.bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f'✅ <b>Баланс пополнен!</b>\n\n'
                        f'💰 Сумма: <b>{payment.amount_kopeks // 100} ₽</b>\n'
                        f'💳 Способ оплаты: {settings.YOOMONEY_DISPLAY_NAME}'
                    ),
                    parse_mode='HTML',
                )
            except Exception as notify_err:
                logger.warning('Не удалось отправить уведомление ЮMoney', error=notify_err)

        try:
            from app.handlers.balance.main import handle_successful_topup_with_cart
            await handle_successful_topup_with_cart(
                user.id, payment.amount_kopeks, self.bot, db
            )
        except Exception:
            pass

        return True
