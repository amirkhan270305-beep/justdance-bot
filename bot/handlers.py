from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)

from .ai import SalesAgent
from .catalog import Catalog, Product, money
from .config import Settings
from .db import Database
from .faq import faq_reply

log = logging.getLogger(__name__)

CURRENCY = "KZT"
KZT_MINOR_UNITS = 100  # Telegram принимает сумму в тиынах
HANDOFF_COOLDOWN = 600
UNDECIDED = "Пока не определились"

client_router = Router(name="client")
client_router.message.filter(F.chat.type == "private")
admin_router = Router(name="admin")


@dataclass
class Services:
    settings: Settings
    catalog: Catalog
    db: Database
    agent: SalesAgent
    locks: dict[int, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock))
    last_handoff: dict[int, float] = field(default_factory=dict)


class Form(StatesGroup):
    age = State()
    direction = State()
    name = State()
    phone = State()
    callback_phone = State()


class Buy(CallbackData, prefix="buy"):
    product_id: str


class Menu(CallbackData, prefix="menu"):
    action: str


class Direction(CallbackData, prefix="dir"):
    index: int


class AdminAction(CallbackData, prefix="adm"):
    action: str
    order_id: int


def button(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def buy_button(product: Product) -> InlineKeyboardButton:
    return button(f"{product.title} · {money(product.price)}", Buy(product_id=product.id))


def main_menu(catalog: Catalog) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [buy_button(catalog.trial)],
            [button("Абонементы", Menu(action="subs"))],
            [button("Направления и расписание", Menu(action="info"))],
            [button("Адрес и контакты", Menu(action="contacts"))],
            [button("Связаться с менеджером", Menu(action="manager"))],
        ]
    )


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def order_keyboard(order: sqlite3.Row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("✅ Оплата получена", AdminAction(action="paid", order_id=order["id"])),
                button("❌ Отклонить", AdminAction(action="reject", order_id=order["id"])),
            ]
        ]
    )


def normalize_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "7":
        return "+7" + digits
    return None


def consent_text(services: Services) -> str:
    text = "Отправляя номер, вы соглашаетесь на обработку персональных данных для связи по заявке."
    if services.settings.privacy_policy_url:
        text += f"\nПолитика: {services.settings.privacy_policy_url}"
    return text


def client_card(services: Services, tg_id: int) -> str:
    user = services.db.get_user(tg_id)
    if user is None:
        return f"Telegram ID: {tg_id}"
    lines = [
        f"Родитель: {user['parent_name'] or 'не указан'}",
        f"Телефон: {user['phone'] or 'не указан'}",
        f"Возраст ребёнка: {user['child_age'] or 'не указан'}",
        f"Направление: {user['direction'] or 'не указано'}",
        f"Telegram: @{user['username']}" if user["username"] else f"Telegram ID: {tg_id}",
    ]
    return "\n".join(lines)


async def notify_admin(
    bot: Bot, services: Services, text: str, markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    try:
        await bot.send_message(services.settings.admin_chat_id, text, reply_markup=markup)
    except TelegramAPIError:
        log.exception("Cannot notify admin chat")


async def notify_client(bot: Bot, tg_id: int, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> bool:
    try:
        await bot.send_message(tg_id, text, reply_markup=markup)
        return True
    except TelegramAPIError:
        log.warning("Cannot message client %s", tg_id, exc_info=True)
        return False


async def hand_off(bot: Bot, services: Services, user: User, reason: str) -> None:
    moment = time.monotonic()
    previous = services.last_handoff.get(user.id)
    if previous is not None and moment - previous < HANDOFF_COOLDOWN:
        return
    services.last_handoff[user.id] = moment
    await notify_admin(bot, services, f"💬 Нужен менеджер\n{reason}\n\n{client_card(services, user.id)}")


async def ask_callback_phone_if_needed(message: Message, services: Services, state: FSMContext, user: User) -> None:
    profile = services.db.get_user(user.id)
    if profile is not None and profile["phone"]:
        return
    await state.set_state(Form.callback_phone)
    await message.answer(
        f"Чтобы менеджер мог связаться с вами, оставьте номер телефона.\n{consent_text(services)}",
        reply_markup=contact_keyboard(),
    )


# --- Команды -----------------------------------------------------------------


@client_router.message(CommandStart())
@client_router.message(Command("menu"))
async def start(message: Message, services: Services, state: FSMContext) -> None:
    await state.clear()
    services.db.touch_user(message.from_user.id, message.from_user.username)
    trial = services.catalog.trial
    old_price = services.catalog.data["trial"].get("old_price")
    price = f"{money(trial.price)} вместо {money(int(old_price))}" if old_price else money(trial.price)
    credit_note = (
        " Если после него купите абонемент, стоимость пробного засчитаем." if services.catalog.trial_credit else ""
    )
    await message.answer(
        "Здравствуйте! Это танцевальная студия Just Dance в Алматы, занятия для детей и подростков "
        f"{services.catalog.min_age}–{services.catalog.max_age} лет.\n\n"
        f"Первое пробное занятие: {price}.{credit_note}\n\n"
        "Выберите нужный пункт или напишите вопрос своими словами. "
        "На вопросы отвечает ИИ-помощник студии, при необходимости подключится менеджер.",
        reply_markup=main_menu(services.catalog),
    )


@client_router.message(Command("cancel"))
async def cancel(message: Message, services: Services, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, отменили.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Чем ещё помочь?", reply_markup=main_menu(services.catalog))


@client_router.message(Command("forget"))
async def forget(message: Message, services: Services, state: FSMContext) -> None:
    await state.clear()
    services.db.forget_user(message.from_user.id)
    await message.answer(
        "Ваши контакты и переписка с ботом удалены. Сведения об оплатах сохраняются для учёта.",
        reply_markup=ReplyKeyboardRemove(),
    )


@admin_router.message(Command("stats"))
async def stats(message: Message, services: Services) -> None:
    if message.from_user.id not in services.settings.admin_ids:
        return
    s = services.db.stats()
    await message.answer(
        f"Пользователи: {s['users']}\n"
        f"Оставили контакты: {s['leads']}\n"
        f"Оплатили пробное: {s['trial_paid']}\n"
        f"Были на пробном: {s['trial_done']}\n"
        f"Купили абонемент: {s['subs_paid']}\n"
        f"Выручка: {money(s['revenue'])}\n\n"
        "Список заявок: /leads"
    )


@admin_router.message(Command("leads"))
async def leads(message: Message, services: Services) -> None:
    if message.from_user.id not in services.settings.admin_ids:
        return
    rows = services.db.leads(limit=30)
    if not rows:
        await message.answer("Заявок пока нет.")
        return
    lines = []
    for n, row in enumerate(rows, 1):
        who = f"@{row['username']}" if row["username"] else f"ID {row['tg_id']}"
        lines.append(
            f"{n}. {row['parent_name'] or 'без имени'}, {row['phone'] or 'без телефона'}, {who}\n"
            f"   Ребёнку: {row['child_age'] or '?'} лет, {row['direction'] or 'направление не выбрано'}\n"
            f"   Заказ: {row['last_order'] or 'нет'}"
        )
    for part in split_text("Последние заявки:\n\n" + "\n".join(lines)):
        await message.answer(part)


# --- Меню ---------------------------------------------------------------------


@client_router.callback_query(Menu.filter())
async def menu(
    callback: CallbackQuery, callback_data: Menu, services: Services, state: FSMContext, bot: Bot
) -> None:
    await callback.answer()
    services.db.touch_user(callback.from_user.id, callback.from_user.username)
    catalog = services.catalog
    if callback.message is None:
        return
    if callback_data.action == "info":
        await callback.message.answer(catalog.directions_text(), reply_markup=main_menu(catalog))
    elif callback_data.action == "contacts":
        await callback.message.answer(catalog.contacts_text(), reply_markup=main_menu(catalog))
    elif callback_data.action == "subs":
        await show_subscriptions(callback.message, services)
    elif callback_data.action == "manager":
        await hand_off(bot, services, callback.from_user, "Клиент нажал «Связаться с менеджером»")
        await callback.message.answer(f"Передали ваш запрос менеджеру. Телефон студии: {catalog.studio['phone']}")
        await ask_callback_phone_if_needed(callback.message, services, state, callback.from_user)


async def show_subscriptions(message: Message, services: Services) -> None:
    catalog = services.catalog
    if not catalog.subscriptions:
        await message.answer(
            "Абонемент подбирается под возраст и группу ребёнка, стоимость подскажет менеджер. "
            "Начать лучше с пробного занятия.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [buy_button(catalog.trial)],
                    [button("Узнать стоимость у менеджера", Menu(action="manager"))],
                ]
            ),
        )
        return
    lines = [f"• {p.title}: {money(p.price)}. {p.description}".strip() for p in catalog.subscriptions]
    if catalog.trial_credit:
        lines.append(f"\nПосле пробного занятия его стоимость ({money(catalog.trial.price)}) засчитывается в абонемент.")
    await message.answer(
        "Абонементы:\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[buy_button(p)] for p in catalog.subscriptions]),
    )


# --- Покупка и анкета ---------------------------------------------------------


@client_router.callback_query(Buy.filter())
async def buy(
    callback: CallbackQuery, callback_data: Buy, services: Services, state: FSMContext, bot: Bot
) -> None:
    await callback.answer()
    product = services.catalog.products.get(callback_data.product_id)
    if product is None or callback.message is None:
        await bot.send_message(callback.from_user.id, "Это предложение больше не действует. Откройте /menu.")
        return
    services.db.touch_user(callback.from_user.id, callback.from_user.username)
    profile = services.db.get_user(callback.from_user.id)
    if all(profile[key] for key in ("parent_name", "child_age", "direction", "phone", "consent_at")):
        await state.clear()
        await checkout(bot, services, callback.from_user, product)
        return
    await state.set_state(Form.age)
    await state.update_data(product_id=product.id)
    await callback.message.answer(f"Оформляем: {product.title}.\nСколько лет ребёнку? Напишите числом.")


@client_router.message(Form.age, F.text, ~F.text.startswith("/"))
async def form_age(message: Message, services: Services, state: FSMContext, bot: Bot) -> None:
    catalog = services.catalog
    if not message.text.strip().isdigit():
        await message.answer("Напишите возраст числом, например 8.")
        return
    age = int(message.text.strip())
    if not catalog.min_age <= age <= catalog.max_age:
        await state.clear()
        await hand_off(bot, services, message.from_user, f"Вопрос по возрасту ребёнка: {age}")
        await message.answer(
            f"Группы рассчитаны на {catalog.min_age}–{catalog.max_age} лет. Передали вопрос менеджеру, "
            f"также можно позвонить: {catalog.studio['phone']}"
        )
        return
    await state.update_data(child_age=age)
    await state.set_state(Form.direction)
    rows = [[button(d["name"], Direction(index=i))] for i, d in enumerate(catalog.directions)]
    rows.append([button(UNDECIDED, Direction(index=-1))])
    await message.answer("Какое направление интересно?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@client_router.callback_query(Form.direction, Direction.filter())
async def form_direction(
    callback: CallbackQuery, callback_data: Direction, services: Services, state: FSMContext
) -> None:
    await callback.answer()
    directions = services.catalog.directions
    index = callback_data.index
    if index != -1 and not 0 <= index < len(directions):
        return
    await state.update_data(direction=UNDECIDED if index == -1 else directions[index]["name"])
    await state.set_state(Form.name)
    await callback.message.answer("Как к вам обращаться? Напишите имя родителя.")


@client_router.message(Form.name, F.text, ~F.text.startswith("/"))
async def form_name(message: Message, services: Services, state: FSMContext) -> None:
    name = " ".join(message.text.split())
    if not 2 <= len(name) <= 60:
        await message.answer("Напишите, пожалуйста, имя.")
        return
    await state.update_data(parent_name=name)
    await state.set_state(Form.phone)
    await message.answer(
        f"Оставьте номер телефона для связи.\n{consent_text(services)}",
        reply_markup=contact_keyboard(),
    )


def phone_from(message: Message) -> Optional[str]:
    if message.contact is not None:
        if message.contact.user_id != message.from_user.id:
            return None
        return normalize_phone(message.contact.phone_number)
    if message.text and not message.text.startswith("/"):
        return normalize_phone(message.text)
    return None


@client_router.message(Form.phone, F.contact | (F.text & ~F.text.startswith("/")))
async def form_phone(message: Message, services: Services, state: FSMContext, bot: Bot) -> None:
    phone = phone_from(message)
    if phone is None:
        await message.answer("Нажмите кнопку ниже или напишите номер в формате +7 7XX XXX XX XX.")
        return
    data = await state.get_data()
    await state.clear()
    product = services.catalog.products.get(data.get("product_id", ""))
    services.db.touch_user(message.from_user.id, message.from_user.username)
    services.db.save_profile(
        message.from_user.id, data["parent_name"], data["child_age"], data["direction"], phone
    )
    await message.answer("Спасибо, записали!", reply_markup=ReplyKeyboardRemove())
    if product is None:
        await message.answer("Это предложение больше не действует.", reply_markup=main_menu(services.catalog))
        return
    await checkout(bot, services, message.from_user, product)


@client_router.message(Form.callback_phone, F.contact | (F.text & ~F.text.startswith("/")))
async def form_callback_phone(message: Message, services: Services, state: FSMContext, bot: Bot) -> None:
    phone = phone_from(message)
    if phone is None:
        if message.contact is not None:
            await message.answer("Отправьте, пожалуйста, свой номер или напишите его текстом.")
            return
        # Клиент не обязан оставлять номер: обычный вопрос продолжает диалог.
        await state.clear()
        await chat(message, services, state, bot)
        return
    await state.clear()
    services.db.touch_user(message.from_user.id, message.from_user.username)
    services.db.save_phone(message.from_user.id, phone)
    await message.answer("Спасибо! Менеджер свяжется с вами.", reply_markup=ReplyKeyboardRemove())
    await notify_admin(
        bot, services, f"📞 Клиент оставил номер\n\n{client_card(services, message.from_user.id)}"
    )


# --- Оплата -------------------------------------------------------------------


async def checkout(bot: Bot, services: Services, user: User, product: Product) -> None:
    settings = services.settings
    credit = 0
    if product.kind == "subscription" and services.catalog.trial_credit:
        credit = services.db.trial_credit(user.id)
        if credit >= product.price:
            credit = 0
    order, created = services.db.get_or_create_order(user.id, product, product.price - credit)
    price_line = money(order["amount"])
    if credit:
        price_line = f"{money(product.price)} − {money(credit)} за пробное = {money(order['amount'])}"
    if order["status"] == "review":
        await bot.send_message(user.id, "Ваш чек уже на проверке. Менеджер скоро подтвердит оплату.")
        return

    if settings.payment_mode == "telegram":
        if created:
            await notify_admin(
                bot, services, f"🆕 Заказ №{order['id']}: {order['title']}, ожидает оплаты\n\n"
                f"{client_card(services, user.id)}",
            )
        try:
            await bot.send_invoice(
                chat_id=user.id,
                title=product.title[:32],
                payload=f"order:{order['id']}",
                provider_token=settings.telegram_provider_token,
                currency=CURRENCY,
                description=(f"{product.description} {'Стоимость пробного учтена.' if credit else ''}").strip()[:255]
                or product.title,
                prices=[LabeledPrice(label=product.title[:32], amount=order["amount"] * KZT_MINOR_UNITS)],
            )
        except TelegramAPIError:
            log.exception("Cannot send invoice for order %s", order["id"])
            await notify_admin(
                bot,
                services,
                f"⚠️ Не удалось выставить счёт по заказу №{order['id']}. Проверьте платёжного провайдера.\n\n"
                f"{client_card(services, user.id)}",
            )
            await bot.send_message(user.id, "Сейчас не получилось открыть оплату. Менеджер свяжется с вами.")
        return

    if created:
        await notify_admin(
            bot,
            services,
            f"🆕 Заказ №{order['id']}: {order['title']}, {price_line}\n"
            "Подтвердите, только когда деньги поступили.\n\n"
            f"{client_card(services, user.id)}",
            order_keyboard(order),
        )
    amount = price_line
    if settings.kaspi_pay_url:
        await bot.send_message(
            user.id,
            f"Заказ №{order['id']}: {order['title']}\nК оплате: {amount}\n\n"
            "Оплатите по кнопке и пришлите сюда скриншот или чек. "
            "После проверки бот подтвердит заказ.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Оплатить в Kaspi", url=settings.kaspi_pay_url)]]
            ),
        )
    else:
        await bot.send_message(
            user.id,
            f"Заказ №{order['id']}: {order['title']}\nК оплате: {amount}\n\n"
            "Менеджер отправит счёт Kaspi на ваш номер. После оплаты бот подтвердит заказ.",
        )


def invoice_order(services: Services, payload: str, tg_id: int, currency: str, total: int) -> Optional[sqlite3.Row]:
    match = re.fullmatch(r"order:(\d+)", payload)
    if match is None:
        return None
    order = services.db.get_order(int(match.group(1)))
    if (
        order is None
        or order["tg_id"] != tg_id
        or currency != CURRENCY
        or total != order["amount"] * KZT_MINOR_UNITS
    ):
        return None
    return order


@client_router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, services: Services) -> None:
    order = invoice_order(
        services, query.invoice_payload, query.from_user.id, query.currency, query.total_amount
    )
    if order is None or order["status"] != "pending":
        await query.answer(ok=False, error_message="Заказ устарел. Откройте /menu и оформите его заново.")
        return
    await query.answer(ok=True)


@client_router.message(F.successful_payment)
async def successful_payment(message: Message, services: Services, bot: Bot) -> None:
    payment = message.successful_payment
    order = invoice_order(
        services, payment.invoice_payload, message.from_user.id, payment.currency, payment.total_amount
    )
    if order is None:
        log.error("Payment does not match an order: %s", payment.invoice_payload)
        await notify_admin(
            bot,
            services,
            f"⚠️ Платёж не совпал с заказом, проверьте вручную.\n"
            f"Сумма: {payment.total_amount / KZT_MINOR_UNITS:.0f} {payment.currency}\n"
            f"Код платежа: {payment.telegram_payment_charge_id}\n\n"
            f"{client_card(services, message.from_user.id)}",
        )
        await message.answer("Платёж получен, менеджер проверит его и свяжется с вами.")
        return
    if services.db.mark_paid(order["id"], payment.telegram_payment_charge_id):
        await after_paid(bot, services, services.db.get_order(order["id"]))


async def after_paid(bot: Bot, services: Services, order: sqlite3.Row) -> None:
    if order["kind"] == "trial":
        client_text = (
            "Оплата подтверждена, спасибо! Менеджер свяжется с вами и подберёт удобное время пробного занятия.\n"
            f"Адрес: {services.catalog.studio['address']}"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[button("Пробное проведено", AdminAction(action="trial_done", order_id=order["id"]))]]
        )
    else:
        client_text = f"Оплата подтверждена, спасибо! {order['title']} оформлен. Менеджер свяжется с вами."
        markup = None
    delivered = await notify_client(bot, order["tg_id"], client_text)
    warning = "" if delivered else "\n⚠️ Клиенту не удалось отправить сообщение, свяжитесь с ним сами."
    await notify_admin(
        bot,
        services,
        f"💰 Оплачен заказ №{order['id']}: {order['title']}, {money(order['amount'])}{warning}\n\n"
        f"{client_card(services, order['tg_id'])}",
        markup,
    )


@client_router.message(F.photo | F.document)
async def receipt(message: Message, services: Services, bot: Bot) -> None:
    order = None
    if services.settings.payment_mode == "manual":
        order = services.db.latest_pending_order(message.from_user.id)
    if order is None:
        await message.answer("Файл получен, но открытого заказа нет. Напишите вопрос текстом или откройте /menu.")
        return
    if not services.db.mark_review(order["id"]):
        await message.answer("Этот заказ уже обработан.")
        return
    await message.answer("Чек получен. Менеджер проверит оплату, и бот сообщит результат.")
    try:
        await bot.copy_message(services.settings.admin_chat_id, message.chat.id, message.message_id)
    except TelegramAPIError:
        log.exception("Cannot copy receipt to admin chat")
    await notify_admin(
        bot,
        services,
        f"🧾 Чек по заказу №{order['id']}: {order['title']}, {money(order['amount'])}\n"
        "Чек не является подтверждением: проверьте поступление денег в Kaspi.\n\n"
        f"{client_card(services, order['tg_id'])}",
        order_keyboard(order),
    )


# --- Действия менеджера -------------------------------------------------------


@admin_router.callback_query(AdminAction.filter())
async def admin_action(
    callback: CallbackQuery, callback_data: AdminAction, services: Services, bot: Bot
) -> None:
    if callback.from_user.id not in services.settings.admin_ids:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    db = services.db
    order = db.get_order(callback_data.order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    if callback_data.action == "paid":
        changed = db.mark_paid(order["id"])
        if changed:
            await after_paid(bot, services, db.get_order(order["id"]))
    elif callback_data.action == "reject":
        changed = db.mark_rejected(order["id"])
        if changed:
            await notify_client(
                bot,
                order["tg_id"],
                f"Не удалось подтвердить оплату заказа №{order['id']}. "
                f"Свяжитесь с нами: {services.catalog.studio['phone']}",
            )
    elif callback_data.action == "trial_done":
        changed = db.mark_trial_done(order["id"])
        if changed:
            await send_after_trial(bot, services, order)
    else:
        changed = False

    await callback.answer("Готово" if changed else "Заказ уже обработан")
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            log.debug("Cannot remove admin buttons", exc_info=True)


async def send_after_trial(bot: Bot, services: Services, order: sqlite3.Row) -> None:
    subscriptions = services.catalog.subscriptions
    if subscriptions:
        await notify_client(
            bot,
            order["tg_id"],
            "Спасибо, что пришли на пробное занятие! Чтобы ребёнок продолжил заниматься в группе, "
            "выберите абонемент"
            + (
                f". Стоимость пробного ({money(order['amount'])}) засчитаем, доплатите только разницу:"
                if services.catalog.trial_credit
                else ":"
            ),
            InlineKeyboardMarkup(inline_keyboard=[[buy_button(p)] for p in subscriptions]),
        )
    else:
        await notify_client(
            bot,
            order["tg_id"],
            "Спасибо, что пришли на пробное занятие! Менеджер свяжется с вами и подберёт подходящий абонемент.",
        )


# --- Свободный диалог с ИИ ----------------------------------------------------


def split_text(text: str, size: int = 4000) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


@client_router.message(F.text, ~F.text.startswith("/"), ~F.successful_payment)
async def chat(message: Message, services: Services, state: FSMContext, bot: Bot) -> None:
    if await state.get_state() is not None:
        await message.answer("Сначала ответьте на вопрос выше или отправьте /cancel, чтобы отменить.")
        return
    user = message.from_user
    services.db.touch_user(user.id, user.username)
    text = message.text.strip()[:2000]

    async with services.locks[user.id]:
        if services.agent.client is None:
            reply = faq_reply(text, services.catalog)
        else:
            if not services.db.consume_ai_quota(user.id, services.settings.ai_daily_limit):
                await hand_off(bot, services, user, "Клиент исчерпал дневной лимит сообщений ИИ")
                await message.answer(f"Передали диалог менеджеру. Телефон студии: {services.catalog.studio['phone']}")
                await ask_callback_phone_if_needed(message, services, state, user)
                return
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            reply = await services.agent.reply(services.db.history(user.id), text)
        if not reply.failed:
            services.db.add_exchange(user.id, text, reply.text)

    products = [services.catalog.products[pid] for pid in reply.product_ids]
    markup = InlineKeyboardMarkup(inline_keyboard=[[buy_button(p)] for p in products]) if products else None
    parts = split_text(reply.text)
    for part in parts[:-1]:
        await message.answer(part)
    await message.answer(parts[-1], reply_markup=markup)

    if reply.handoff:
        await hand_off(bot, services, user, f"Запрос: {reply.handoff}\nСообщение клиента: {text[:500]}")
        await ask_callback_phone_if_needed(message, services, state, user)


@client_router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Откройте /menu.")


@client_router.message()
async def other(message: Message, services: Services) -> None:
    await message.answer("Напишите вопрос текстом или выберите пункт меню.", reply_markup=main_menu(services.catalog))
