"""Ответы на типовые вопросы по ключевым словам, когда ИИ не подключён."""

from __future__ import annotations

import re
from typing import Callable, Optional

from .ai import AgentReply
from .catalog import Catalog, money


def _prices(c: Catalog) -> str:
    lines = [f"• {p.title}: {money(p.price)}. {p.description}".strip() for p in c.subscriptions]
    lines.append(f"• {c.trial.title}: {money(c.trial.price)}")
    text = "Стоимость занятий:\n" + "\n".join(lines)
    if c.trial_credit:
        text += "\n\nЕсли после пробного занятия вы покупаете абонемент, стоимость пробного засчитывается и доплачивается только разница."
    return text


def _schedule(c: Catalog) -> str:
    groups = "\n".join(f"• {g['ages']}: {g['time']}" for g in c.age_groups)
    return (
        "Расписание. Занятия идут в два потока с одинаковым временем: "
        "понедельник, среда, пятница или вторник, четверг, суббота.\n" + groups
    )


def _ages(c: Catalog) -> str:
    groups = ", ".join(g["ages"] for g in c.age_groups)
    return f"Мы занимаемся с детьми и подростками {c.min_age}–{c.max_age} лет. Группы по возрасту: {groups}."


def _trial(c: Catalog) -> str:
    text = (
        f"Пробное занятие стоит {money(c.trial.price)}. На нём ребёнок знакомится с тренером и группой "
        "и пробует направление."
    )
    if c.trial_credit:
        text += " Если после него купите абонемент, стоимость пробного засчитаем."
    return text + " Записаться можно по кнопке ниже."


def _rules(c: Catalog) -> str:
    return (
        "Абонемент действует 1 месяц. Про заморозку, пропуски и возврат точно подскажет менеджер, "
        f"я передал ему ваш вопрос. Телефон студии: {c.studio['phone']}"
    )


def _greeting(c: Catalog) -> str:
    return (
        "Здравствуйте! Я помощник студии Just Dance. Спросите о ценах, расписании, возрасте, "
        "направлениях или адресе, либо выберите пункт меню."
    )


def _contacts(c: Catalog) -> str:
    return c.contacts_text()


def _directions(c: Catalog) -> str:
    return c.directions_text()


# (ключевые слова, ответ, какие кнопки показать: "all" / "trial" / "", передать менеджеру)
INTENTS: list[tuple[tuple[str, ...], Callable[[Catalog], str], str, bool]] = [
    (("замороз", "пропуск", "пропуст", "возврат", "вернуть деньги", "болеет", "заболел"), _rules, "", True),
    (("цен", "стоим", "скольк", "прайс", "абонем", "тенге", "₸", "оплат", "баға", "қанша"), _prices, "all", False),
    (("распис", "время", "времен", "когда", "дни", "во сколько", "график", "кесте"), _schedule, "", False),
    (("пробн", "записат", "запись", "попроб", "первое занят"), _trial, "trial", False),
    (("адрес", "где ", "где?", "находит", "добрат", "карт", "2гис", "2gis", "мекенжай"), _contacts, "", False),
    (("телефон", "номер", "позвон", "инстаграм", "instagram", "связат"), _contacts, "", False),
    (("направлен", "стил", "какие танц", "контемп", "эстрад", "народн", "детские танц", "чем занима",
      "что делают", "растяжк", "нейрогимнаст"), _directions, "", False),
    (("возраст", "лет", "годик", "года", "жаста"), _ages, "", False),
    (("привет", "здравств", "салем", "сәлем", "добрый", "hello"), _greeting, "", False),
]

UNKNOWN = (
    "Точного ответа на этот вопрос у меня нет, поэтому я передал его менеджеру. "
    "Пока можно посмотреть цены, расписание и адрес в меню: /menu"
)


def faq_reply(text: str, catalog: Catalog) -> AgentReply:
    query = " " + re.sub(r"\s+", " ", text.lower()) + " "
    parts: list[str] = []
    used: set[Callable[[Catalog], str]] = set()
    offers: set[str] = set()
    handoff: Optional[str] = None
    for keywords, answer, offer, to_manager in INTENTS:
        if len(parts) == 2:
            break
        if answer in used or not any(k in query for k in keywords):
            continue
        if answer is _greeting and parts:
            continue
        used.add(answer)
        parts.append(answer(catalog))
        if offer:
            offers.add(offer)
        if to_manager:
            handoff = "Вопрос о правилах абонемента"
    if not parts:
        return AgentReply(UNKNOWN, handoff="Вопрос, на который у бота нет ответа")
    product_ids: list[str] = []
    if offers:
        product_ids.append(catalog.trial.id)
    if "all" in offers:
        product_ids.extend(p.id for p in catalog.subscriptions)
    return AgentReply("\n\n".join(parts), product_ids=product_ids, handoff=handoff)
