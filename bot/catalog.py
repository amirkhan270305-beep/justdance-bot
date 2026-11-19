from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    price: int
    description: str
    kind: str  # "trial" или "subscription"


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₸"


class Catalog:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.studio: dict[str, Any] = data["studio"]
        self.directions: list[dict[str, str]] = data["directions"]
        self.age_groups: list[dict[str, str]] = data["age_groups"]
        self.min_age = int(self.studio["min_age"])
        self.max_age = int(self.studio["max_age"])
        self.trial = self._product(data["trial"], "trial")
        self.trial_credit = bool(data["trial"].get("credit_towards_subscription"))
        self.subscriptions = [
            self._product(item, "subscription") for item in data.get("subscriptions") or []
        ]
        self.products = {p.id: p for p in [self.trial, *self.subscriptions]}
        if len(self.products) != 1 + len(self.subscriptions):
            raise ValueError("В catalog.yaml повторяется id товара")

    @staticmethod
    def _product(item: dict[str, Any], kind: str) -> Product:
        price = item.get("price")
        if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
            raise ValueError(f"Некорректная цена у товара {item.get('id')!r}")
        return Product(str(item["id"]), str(item["title"]), price, str(item.get("description", "")), kind)

    @classmethod
    def load(cls, path: str) -> "Catalog":
        with open(path, encoding="utf-8") as handle:
            return cls(yaml.safe_load(handle))

    def contacts_text(self) -> str:
        s = self.studio
        return (
            f"📍 {s['address']}\n"
            f"Карта: {s['map_url']}\n"
            f"📞 {s['phone']}\n"
            f"Instagram: {s['instagram']}"
        )

    def directions_text(self) -> str:
        directions = "\n".join(f"• {d['name']}: {d['about']}" for d in self.directions)
        ages = "\n".join(f"• {a['ages']}: {a['time']}" for a in self.age_groups)
        text = f"Направления:\n{directions}"
        content = self.data.get("lesson_content") or []
        if content:
            text += "\n\nНа занятиях также:\n" + "\n".join(f"• {item}" for item in content)
        text += (
            "\n\nГруппы и время. Занятия идут в два потока: пн, ср, пт или вт, чт, сб.\n" + ages
        )
        return text

    def prompt_text(self) -> str:
        """Факты для ИИ. Порядок стабилен, чтобы работало кэширование запроса."""
        missing = "НЕТ ДАННЫХ — уточнит менеджер"
        s = self.studio
        lines = [
            f"Студия: {s['name']}. {s['description']}. Город: {s['city']}.",
            f"Адрес: {s['address']}. Карта: {s['map_url']}.",
            f"Телефон: {s['phone']}. Instagram: {s['instagram']}.",
            f"Позиционирование: {self.data.get('positioning', '')}",
            "Преимущества: " + "; ".join(self.data.get("advantages") or []) + ".",
            "",
            self.directions_text(),
            "",
            "Товары, доступные для покупки (product_id — название — цена — описание):",
        ]
        for product in self.products.values():
            lines.append(
                f"- {product.id} — {product.title} — {money(product.price)} — {product.description}"
            )
        old_price = self.data["trial"].get("old_price")
        if old_price:
            lines.append(f"Пробное занятие обычно стоит {money(int(old_price))}, сейчас цена со скидкой.")
        if not self.subscriptions:
            lines.append("Абонементы и их цены: " + missing + ".")
        for title, key in (
            ("Расписание", "schedule"),
            ("Правила (заморозка, пропуски, срок, возврат)", "rules"),
            ("Другие акции и скидки", "promotions"),
            ("Тренеры", "teachers"),
        ):
            lines.append(f"{title}: {self.data.get(key) or missing}.")
        faq = self.data.get("faq") or []
        if faq:
            lines.append("Частые вопросы:")
            lines.extend(f"В: {item['q']}\nО: {item['a']}" for item in faq)
        return "\n".join(lines)
