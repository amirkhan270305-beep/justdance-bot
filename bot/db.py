from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .catalog import Product

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    first_seen TEXT NOT NULL,
    parent_name TEXT,
    child_age INTEGER,
    direction TEXT,
    phone TEXT,
    consent_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_user ON messages (tg_id, id);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'review', 'paid', 'rejected')),
    telegram_charge_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    trial_done_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_usage (
    tg_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (tg_id, day)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def touch_user(self, tg_id: int, username: Optional[str]) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO users (tg_id, username, first_seen) VALUES (?, ?, ?)
                   ON CONFLICT (tg_id) DO UPDATE SET username = excluded.username""",
                (tg_id, username, now()),
            )

    def get_user(self, tg_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()

    def save_profile(self, tg_id: int, parent_name: str, child_age: int, direction: str, phone: str) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE users SET parent_name = ?, child_age = ?, direction = ?, phone = ?, consent_at = ?
                   WHERE tg_id = ?""",
                (parent_name, child_age, direction, phone, now(), tg_id),
            )

    def save_phone(self, tg_id: int, phone: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE users SET phone = ?, consent_at = ? WHERE tg_id = ?",
                (phone, now(), tg_id),
            )

    def forget_user(self, tg_id: int) -> None:
        """Удаляет контакты и переписку. Заказы остаются для учёта оплат."""
        with self.conn:
            self.conn.execute("DELETE FROM messages WHERE tg_id = ?", (tg_id,))
            self.conn.execute("DELETE FROM ai_usage WHERE tg_id = ?", (tg_id,))
            self.conn.execute(
                """UPDATE users SET username = NULL, parent_name = NULL, child_age = NULL,
                   direction = NULL, phone = NULL, consent_at = NULL WHERE tg_id = ?""",
                (tg_id,),
            )

    def history(self, tg_id: int, limit: int = 20) -> list[dict[str, str]]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE tg_id = ? ORDER BY id DESC LIMIT ?",
            (tg_id, limit),
        ).fetchall()
        items = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
        while items and items[0]["role"] != "user":
            items.pop(0)
        return items

    def add_exchange(self, tg_id: int, user_text: str, assistant_text: str) -> None:
        stamp = now()
        with self.conn:
            self.conn.executemany(
                "INSERT INTO messages (tg_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(tg_id, "user", user_text, stamp), (tg_id, "assistant", assistant_text, stamp)],
            )

    def consume_ai_quota(self, tg_id: int, limit: int) -> bool:
        day = datetime.now(timezone.utc).date().isoformat()
        with self.conn:
            cursor = self.conn.execute(
                """INSERT INTO ai_usage (tg_id, day, count) VALUES (?, ?, 1)
                   ON CONFLICT (tg_id, day) DO UPDATE SET count = count + 1
                   WHERE count < ?""",
                (tg_id, day, limit),
            )
        return cursor.rowcount == 1

    def trial_credit(self, tg_id: int) -> int:
        """Сумма оплаченного пробного, если после него ещё не покупали абонемент."""
        row = self.conn.execute(
            """SELECT amount, paid_at FROM orders WHERE tg_id = ? AND kind = 'trial' AND status = 'paid'
               ORDER BY paid_at DESC LIMIT 1""",
            (tg_id,),
        ).fetchone()
        if row is None:
            return 0
        used = self.conn.execute(
            """SELECT 1 FROM orders WHERE tg_id = ? AND kind = 'subscription' AND status = 'paid'
               AND paid_at >= ?""",
            (tg_id, row["paid_at"]),
        ).fetchone()
        return 0 if used else int(row["amount"])

    def get_or_create_order(self, tg_id: int, product: Product, amount: int) -> tuple[sqlite3.Row, bool]:
        """Возвращает заказ и признак того, что он создан только что."""
        with self.conn:
            row = self.conn.execute(
                """SELECT * FROM orders WHERE tg_id = ? AND product_id = ? AND amount = ?
                   AND status IN ('pending', 'review') ORDER BY id DESC LIMIT 1""",
                (tg_id, product.id, amount),
            ).fetchone()
            if row:
                return row, False
            cursor = self.conn.execute(
                """INSERT INTO orders (tg_id, product_id, kind, title, amount, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (tg_id, product.id, product.kind, product.title, amount, now()),
            )
        return self.get_order(int(cursor.lastrowid)), True

    def get_order(self, order_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def latest_pending_order(self, tg_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE tg_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (tg_id,),
        ).fetchone()

    def _move(self, order_id: int, sources: Iterable[str], target: str, extra: str = "", args: tuple = ()) -> bool:
        sources = tuple(sources)
        marks = ",".join("?" for _ in sources)
        with self.conn:
            cursor = self.conn.execute(
                f"UPDATE orders SET status = ?{extra} WHERE id = ? AND status IN ({marks})",
                (target, *args, order_id, *sources),
            )
        return cursor.rowcount == 1

    def mark_review(self, order_id: int) -> bool:
        return self._move(order_id, ["pending"], "review")

    def mark_rejected(self, order_id: int) -> bool:
        return self._move(order_id, ["pending", "review"], "rejected")

    def mark_paid(self, order_id: int, charge_id: Optional[str] = None) -> bool:
        """True только у первого подтверждения: повтор не засчитает оплату дважды."""
        try:
            return self._move(
                order_id,
                ["pending", "review"],
                "paid",
                ", paid_at = ?, telegram_charge_id = ?",
                (now(), charge_id),
            )
        except sqlite3.IntegrityError:
            return False

    def mark_trial_done(self, order_id: int) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE orders SET trial_done_at = ?
                   WHERE id = ? AND kind = 'trial' AND status = 'paid' AND trial_done_at IS NULL""",
                (now(), order_id),
            )
        return cursor.rowcount == 1

    def leads(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT u.*, (
                   SELECT o.title || ', ' || o.amount || ' ₸, ' || CASE o.status
                       WHEN 'pending' THEN 'ждёт оплаты' WHEN 'review' THEN 'чек на проверке'
                       WHEN 'paid' THEN 'оплачен' ELSE 'отклонён' END
                   FROM orders o WHERE o.tg_id = u.tg_id ORDER BY o.id DESC LIMIT 1
               ) AS last_order
               FROM users u WHERE u.phone IS NOT NULL
               ORDER BY u.consent_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def stats(self) -> dict[str, int]:
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "leads": one("SELECT COUNT(*) FROM users WHERE phone IS NOT NULL"),
            "trial_paid": one("SELECT COUNT(*) FROM orders WHERE kind = 'trial' AND status = 'paid'"),
            "trial_done": one("SELECT COUNT(*) FROM orders WHERE trial_done_at IS NOT NULL"),
            "subs_paid": one("SELECT COUNT(*) FROM orders WHERE kind = 'subscription' AND status = 'paid'"),
            "revenue": one("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid'"),
        }
