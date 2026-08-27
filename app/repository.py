from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3


MAX_ITEM_NAME_LENGTH = 60


class ListLimitReachedError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShoppingItem:
    id: int
    chat_id: int
    name: str
    is_checked: bool


@dataclass(frozen=True)
class AddItemResult:
    item: ShoppingItem
    created: bool
    reactivated: bool


class ShoppingRepository:
    def __init__(self, database_path: str, max_items_per_list: int):
        self._database_path = Path(database_path)
        self._max_items_per_list = max_items_per_list

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    async def list_items(self, chat_id: int) -> list[ShoppingItem]:
        return await asyncio.to_thread(self._list_items, chat_id)

    async def add_item(
        self,
        chat_id: int,
        name: str,
        created_by: int | None,
    ) -> AddItemResult:
        normalized_name = normalize_item_name(name)
        return await asyncio.to_thread(
            self._add_item,
            chat_id,
            normalized_name,
            created_by,
        )

    async def toggle_item(self, chat_id: int, item_id: int) -> ShoppingItem | None:
        return await asyncio.to_thread(self._toggle_item, chat_id, item_id)

    async def remove_checked(self, chat_id: int) -> int:
        return await asyncio.to_thread(self._remove_checked, chat_id)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS shopping_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        normalized_name TEXT NOT NULL,
                        is_checked INTEGER NOT NULL DEFAULT 0
                            CHECK (is_checked IN (0, 1)),
                        created_by INTEGER,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (chat_id, normalized_name)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_shopping_items_chat
                    ON shopping_items (chat_id, is_checked, id)
                    """
                )

    def _list_items(self, chat_id: int) -> list[ShoppingItem]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, name, is_checked
                FROM shopping_items
                WHERE chat_id = ?
                ORDER BY is_checked ASC, id ASC
                """,
                (chat_id,),
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def _add_item(
        self,
        chat_id: int,
        name: str,
        created_by: int | None,
    ) -> AddItemResult:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT id, chat_id, name, is_checked
                    FROM shopping_items
                    WHERE chat_id = ? AND normalized_name = ?
                    """,
                    (chat_id, name.casefold()),
                ).fetchone()
                if existing is not None:
                    if existing["is_checked"]:
                        connection.execute(
                            """
                            UPDATE shopping_items
                            SET is_checked = 0, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (existing["id"],),
                        )
                        item = ShoppingItem(
                            id=existing["id"],
                            chat_id=existing["chat_id"],
                            name=existing["name"],
                            is_checked=False,
                        )
                        return AddItemResult(item, created=False, reactivated=True)
                    return AddItemResult(
                        _item_from_row(existing),
                        created=False,
                        reactivated=False,
                    )

                count = connection.execute(
                    "SELECT COUNT(*) FROM shopping_items WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()[0]
                if count >= self._max_items_per_list:
                    raise ListLimitReachedError(
                        f"Shopping list is limited to {self._max_items_per_list} items"
                    )

                cursor = connection.execute(
                    """
                    INSERT INTO shopping_items (
                        chat_id,
                        name,
                        normalized_name,
                        created_by
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (chat_id, name, name.casefold(), created_by),
                )
                item = ShoppingItem(
                    id=cursor.lastrowid,
                    chat_id=chat_id,
                    name=name,
                    is_checked=False,
                )
                return AddItemResult(item, created=True, reactivated=False)

    def _toggle_item(self, chat_id: int, item_id: int) -> ShoppingItem | None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT id, chat_id, name, is_checked
                    FROM shopping_items
                    WHERE id = ? AND chat_id = ?
                    """,
                    (item_id, chat_id),
                ).fetchone()
                if row is None:
                    return None

                is_checked = not bool(row["is_checked"])
                connection.execute(
                    """
                    UPDATE shopping_items
                    SET is_checked = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND chat_id = ?
                    """,
                    (int(is_checked), item_id, chat_id),
                )
                return ShoppingItem(
                    id=row["id"],
                    chat_id=row["chat_id"],
                    name=row["name"],
                    is_checked=is_checked,
                )

    def _remove_checked(self, chat_id: int) -> int:
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM shopping_items
                    WHERE chat_id = ? AND is_checked = 1
                    """,
                    (chat_id,),
                )
                return cursor.rowcount


def normalize_item_name(name: str) -> str:
    normalized = " ".join(name.split())
    if not normalized:
        raise ValueError("Item name must not be empty")
    if len(normalized) > MAX_ITEM_NAME_LENGTH:
        raise ValueError(
            f"Item name must not exceed {MAX_ITEM_NAME_LENGTH} characters"
        )
    return normalized


def _item_from_row(row: sqlite3.Row) -> ShoppingItem:
    return ShoppingItem(
        id=row["id"],
        chat_id=row["chat_id"],
        name=row["name"],
        is_checked=bool(row["is_checked"]),
    )
