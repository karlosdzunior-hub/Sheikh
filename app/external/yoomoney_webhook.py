"""Обработчик уведомлений ЮMoney (HTTP-notifications).

ЮMoney отправляет POST-запрос на указанный URL после каждого успешного
платежа. Подпись проверяется по алгоритму SHA-1.

Документация: https://yoomoney.ru/docs/payment-buttons/using-api/notifications
"""
from __future__ import annotations

import hashlib
from typing import Any

import structlog


logger = structlog.get_logger(__name__)


def verify_yoomoney_signature(params: dict[str, Any], notification_secret: str) -> bool:
    """Проверяет подпись уведомления ЮMoney.

    Алгоритм: SHA1(notification_type&operation_id&amount&currency&datetime&sender&codepro&secret&label)
    """
    fields = [
        'notification_type',
        'operation_id',
        'amount',
        'currency',
        'datetime',
        'sender',
        'codepro',
    ]
    parts = [str(params.get(f, '')) for f in fields]
    parts.append(notification_secret)
    parts.append(str(params.get('label', '')))

    check_string = '&'.join(parts)
    computed = hashlib.sha1(check_string.encode('utf-8')).hexdigest()
    received = params.get('sha1_hash', '')

    if computed != received:
        logger.warning(
            'ЮMoney: неверная подпись уведомления',
            expected=computed,
            received=received,
        )
        return False
    return True


async def check_yoomoney_payment_by_label(label: str, token: str) -> dict[str, Any] | None:
    """Проверяет статус платежа через YooMoney Operations History API.

    Returns operation dict or None.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                'https://yoomoney.ru/api/operation-history',
                headers={'Authorization': f'Bearer {token}'},
                data={'label': label},
            )
            if resp.status_code != 200:
                logger.warning('ЮMoney API вернул не 200', status_code=resp.status_code)
                return None
            data = resp.json()
            operations = data.get('operations', [])
            for op in operations:
                if op.get('label') == label and op.get('direction') == 'in':
                    return op
            return None
    except Exception as e:
        logger.error('Ошибка запроса к YooMoney API', error=e)
        return None
