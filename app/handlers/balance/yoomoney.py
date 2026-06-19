import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_yoomoney_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.is_yoomoney_enabled():
        await callback.answer('❌ Оплата через ЮMoney временно недоступна', show_alert=True)
        return

    min_rub = settings.YOOMONEY_MIN_AMOUNT_KOPEKS / 100
    max_rub = settings.YOOMONEY_MAX_AMOUNT_KOPEKS / 100

    await callback.message.edit_text(
        f'💛 <b>Оплата через {settings.YOOMONEY_DISPLAY_NAME}</b>\n\n'
        f'Введите сумму для пополнения от {min_rub:.0f} до {max_rub:,.0f} рублей:'.replace(',', ' '),
        reply_markup=get_back_keyboard(db_user.language),
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='yoomoney')
    await callback.answer()


@error_handler
async def process_yoomoney_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
        await message.answer(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_yoomoney_enabled():
        await message.answer('❌ Оплата через ЮMoney временно недоступна')
        return

    if amount_kopeks < settings.YOOMONEY_MIN_AMOUNT_KOPEKS:
        min_rub = settings.YOOMONEY_MIN_AMOUNT_KOPEKS / 100
        await message.answer(
            f'❌ Минимальная сумма для ЮMoney: {min_rub:.0f} ₽',
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    if amount_kopeks > settings.YOOMONEY_MAX_AMOUNT_KOPEKS:
        max_rub = settings.YOOMONEY_MAX_AMOUNT_KOPEKS / 100
        await message.answer(
            f'❌ Максимальная сумма для ЮMoney: {max_rub:,.0f} ₽'.replace(',', ' '),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    try:
        payment_service = PaymentService(message.bot)

        payment_result = await payment_service.create_yoomoney_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(
                amount_kopeks, telegram_user_id=db_user.telegram_id
            ),
            metadata={
                'user_telegram_id': str(db_user.telegram_id),
                'user_username': db_user.username or '',
                'purpose': 'balance_topup',
            },
        )

        if not payment_result:
            await message.answer('❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.')
            await state.clear()
            return

        payment_url = payment_result.get('payment_url')
        local_payment_id = payment_result['local_payment_id']

        try:
            await message.delete()
        except Exception:
            pass

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=f'💛 Оплатить через {settings.YOOMONEY_DISPLAY_NAME}', url=payment_url)],
                [
                    types.InlineKeyboardButton(
                        text='✅ Я оплатил — проверить',
                        callback_data=f'check_yoomoney_{local_payment_id}',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        await message.answer(
            f'💛 <b>Оплата через {settings.YOOMONEY_DISPLAY_NAME}</b>\n\n'
            f'💰 Сумма: <b>{settings.format_price(amount_kopeks)}</b>\n\n'
            f'📋 <b>Инструкция:</b>\n'
            f'1. Нажмите кнопку «Оплатить»\n'
            f'2. Войдите в кошелёк ЮMoney\n'
            f'3. Подтвердите перевод\n'
            f'4. Вернитесь сюда и нажмите «Я оплатил — проверить»\n\n'
            f'⏱ После оплаты нажмите кнопку для подтверждения.\n'
            f'❓ Проблемы? Обратитесь в {settings.get_support_contact_display_html()}',
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        await state.clear()

    except Exception as e:
        logger.error('Ошибка создания платежа ЮMoney', error=e)
        await message.answer('❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.')
        await state.clear()


@error_handler
async def check_yoomoney_payment_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        local_payment_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Неверный ID платежа', show_alert=True)
        return

    await callback.answer('🔍 Проверяю платёж...', show_alert=False)

    try:
        payment_service = PaymentService(callback.bot)
        result = await payment_service.verify_yoomoney_payment(db=db, local_payment_id=local_payment_id)

        if not result:
            await callback.message.answer('❌ Платёж не найден.')
            return

        if result.get('status') == 'succeeded':
            texts = get_texts(db_user.language)
            await callback.message.answer(
                f'✅ <b>Оплата подтверждена!</b>\n\n'
                f'Баланс пополнен. Текущий баланс: <b>{texts.format_price(db_user.balance_kopeks)}</b>',
                parse_mode='HTML',
            )
        else:
            await callback.answer(
                '⏳ Платёж ещё не поступил. Попробуйте через минуту или обратитесь в поддержку.',
                show_alert=True,
            )

    except Exception as e:
        logger.error('Ошибка проверки платежа ЮMoney', error=e)
        await callback.answer('❌ Ошибка проверки платежа. Попробуйте позже.', show_alert=True)
