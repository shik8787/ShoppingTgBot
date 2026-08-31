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
    list_id: int | None = None


@dataclass(frozen=True)
class AddItemResult:
    item: ShoppingItem
    created: bool
    reactivated: bool


@dataclass(frozen=True)
class CreateListResult:
    list_id: int
    items: tuple[ShoppingItem, ...]
    duplicate_count: int


class ShoppingRepository:
    def __init__(self, database_path: str, max_items_per_list: int):
        self._database_path = Path(database_path)
        self._max_items_per_list = max_items_per_list

    @property
    def max_items_per_list(self) -> int:
        return self._max_items_per_list

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    async def latest_list_id(self, chat_id: int) -> int | None:
        return await asyncio.to_thread(self._latest_list_id, chat_id)

    async def list_items(
        self,
        chat_id: int,
        list_id: int | None = None,
    ) -> list[ShoppingItem]:
        return await asyncio.to_thread(self._list_items, chat_id, list_id)

    async def create_list(
        self,
        chat_id: int,
        names: list[str],
        created_by: int | None,
    ) -> CreateListResult:
        normalized_names = [normalize_item_name(name) for name in names]
        return await asyncio.to_thread(
            self._create_list,
            chat_id,
            normalized_names,
            created_by,
        )

    async def replace_list(
        self,
        chat_id: int,
        list_id: int,
        names: list[str],
        created_by: int | None,
    ) -> CreateListResult | None:
        normalized_names = [normalize_item_name(name) for name in names]
        return await asyncio.to_thread(
            self._replace_list,
            chat_id,
            list_id,
            normalized_names,
            created_by,
        )

    async def add_item(
        self,
        chat_id: int,
        name: str,
        created_by: int | None,
        list_id: int | None = None,
    ) -> AddItemResult:
        normalized_name = normalize_item_name(name)
        return await asyncio.to_thread(
            self._add_item,
            chat_id,
            normalized_name,
            created_by,
            list_id,
        )

    async def toggle_item(
        self,
        chat_id: int,
        item_id: int,
        list_id: int | None = None,
    ) -> ShoppingItem | None:
        return await asyncio.to_thread(
            self._toggle_item,
            chat_id,
            item_id,
            list_id,
        )

    async def remove_checked(
        self,
        chat_id: int,
        list_id: int | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._remove_checked,
            chat_id,
            list_id,
        )

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
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(shopping_items)"
                    )
                }
                if columns and "list_id" not in columns:
                    self._migrate_legacy_schema(connection)
                self._create_schema(connection)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shopping_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_shopping_lists_chat
            ON shopping_lists (chat_id, id DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shopping_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL
                    REFERENCES shopping_lists(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                is_checked INTEGER NOT NULL DEFAULT 0
                    CHECK (is_checked IN (0, 1)),
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (list_id, normalized_name)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_shopping_items_list
            ON shopping_items (list_id, is_checked, id)
            """
        )

    def _migrate_legacy_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP INDEX IF EXISTS ix_shopping_items_chat")
        connection.execute(
            "ALTER TABLE shopping_items RENAME TO shopping_items_legacy"
        )
        self._create_schema(connection)
        chat_ids = connection.execute(
            "SELECT DISTINCT chat_id FROM shopping_items_legacy ORDER BY chat_id"
        ).fetchall()
        for chat in chat_ids:
            chat_id = int(chat["chat_id"])
            cursor = connection.execute(
                "INSERT INTO shopping_lists (chat_id) VALUES (?)",
                (chat_id,),
            )
            connection.execute(
                """
                INSERT INTO shopping_items (
                    id, list_id, chat_id, name, normalized_name, is_checked,
                    created_by, created_at, updated_at
                )
                SELECT
                    id, ?, chat_id, name, normalized_name, is_checked,
                    created_by, created_at, updated_at
                FROM shopping_items_legacy
                WHERE chat_id = ?
                """,
                (cursor.lastrowid, chat_id),
            )
        connection.execute("DROP TABLE shopping_items_legacy")

    @staticmethod
    def _latest_list_id_in_connection(
        connection: sqlite3.Connection,
        chat_id: int,
    ) -> int | None:
        row = connection.execute(
            """
            SELECT id
            FROM shopping_lists
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def _latest_list_id(self, chat_id: int) -> int | None:
        with closing(self._connect()) as connection:
            return self._latest_list_id_in_connection(connection, chat_id)

    def _list_items(
        self,
        chat_id: int,
        list_id: int | None,
    ) -> list[ShoppingItem]:
        with closing(self._connect()) as connection:
            target_list_id = list_id or self._latest_list_id_in_connection(
                connection,
                chat_id,
            )
            if target_list_id is None:
                return []
            rows = connection.execute(
                """
                SELECT id, list_id, chat_id, name, is_checked
                FROM shopping_items
                WHERE chat_id = ? AND list_id = ?
                ORDER BY is_checked ASC, id ASC
                """,
                (chat_id, target_list_id),
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    @staticmethod
    def _deduplicate(names: list[str]) -> tuple[list[str], int]:
        unique_names = []
        seen = set()
        for name in names:
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique_names.append(name)
        return unique_names, len(names) - len(unique_names)

    def _create_list(
        self,
        chat_id: int,
        names: list[str],
        created_by: int | None,
    ) -> CreateListResult:
        unique_names, duplicate_count = self._deduplicate(names)
        self._check_limit(unique_names)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO shopping_lists (chat_id, created_by)
                    VALUES (?, ?)
                    """,
                    (chat_id, created_by),
                )
                list_id = int(cursor.lastrowid)
                items = self._insert_items(
                    connection,
                    list_id,
                    chat_id,
                    unique_names,
                    created_by,
                )
        return CreateListResult(list_id, tuple(items), duplicate_count)

    def _replace_list(
        self,
        chat_id: int,
        list_id: int,
        names: list[str],
        created_by: int | None,
    ) -> CreateListResult | None:
        unique_names, duplicate_count = self._deduplicate(names)
        self._check_limit(unique_names)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute(
                    """
                    SELECT 1 FROM shopping_lists
                    WHERE id = ? AND chat_id = ?
                    """,
                    (list_id, chat_id),
                ).fetchone()
                if exists is None:
                    return None
                connection.execute(
                    "DELETE FROM shopping_items WHERE list_id = ? AND chat_id = ?",
                    (list_id, chat_id),
                )
                items = self._insert_items(
                    connection,
                    list_id,
                    chat_id,
                    unique_names,
                    created_by,
                )
                connection.execute(
                    """
                    UPDATE shopping_lists
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND chat_id = ?
                    """,
                    (list_id, chat_id),
                )
        return CreateListResult(list_id, tuple(items), duplicate_count)

    def _add_item(
        self,
        chat_id: int,
        name: str,
        created_by: int | None,
        list_id: int | None,
    ) -> AddItemResult:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                target_list_id = list_id
                if target_list_id is None:
                    target_list_id = self._latest_list_id_in_connection(
                        connection,
                        chat_id,
                    )
                if target_list_id is None:
                    target_list_id = int(
                        connection.execute(
                            """
                            INSERT INTO shopping_lists (chat_id, created_by)
                            VALUES (?, ?)
                            """,
                            (chat_id, created_by),
                        ).lastrowid
                    )
                elif not self._list_belongs_to_chat(
                    connection,
                    target_list_id,
                    chat_id,
                ):
                    raise ValueError("Shopping list does not belong to this chat")

                existing = connection.execute(
                    """
                    SELECT id, list_id, chat_id, name, is_checked
                    FROM shopping_items
                    WHERE list_id = ? AND normalized_name = ?
                    """,
                    (target_list_id, name.casefold()),
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
                        item = _item_from_row(existing)
                        return AddItemResult(
                            ShoppingItem(
                                item.id,
                                item.chat_id,
                                item.name,
                                False,
                                item.list_id,
                            ),
                            created=False,
                            reactivated=True,
                        )
                    return AddItemResult(
                        _item_from_row(existing),
                        created=False,
                        reactivated=False,
                    )

                count = connection.execute(
                    "SELECT COUNT(*) FROM shopping_items WHERE list_id = ?",
                    (target_list_id,),
                ).fetchone()[0]
                if count >= self._max_items_per_list:
                    raise ListLimitReachedError(
                        f"Shopping list is limited to "
                        f"{self._max_items_per_list} items"
                    )

                cursor = connection.execute(
                    """
                    INSERT INTO shopping_items (
                        list_id, chat_id, name, normalized_name, created_by
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        target_list_id,
                        chat_id,
                        name,
                        name.casefold(),
                        created_by,
                    ),
                )
                return AddItemResult(
                    ShoppingItem(
                        id=cursor.lastrowid,
                        chat_id=chat_id,
                        name=name,
                        is_checked=False,
                        list_id=target_list_id,
                    ),
                    created=True,
                    reactivated=False,
                )

    def _toggle_item(
        self,
        chat_id: int,
        item_id: int,
        list_id: int | None,
    ) -> ShoppingItem | None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                parameters: list[int] = [item_id, chat_id]
                list_filter = ""
                if list_id is not None:
                    list_filter = " AND list_id = ?"
                    parameters.append(list_id)
                row = connection.execute(
                    f"""
                    SELECT id, list_id, chat_id, name, is_checked
                    FROM shopping_items
                    WHERE id = ? AND chat_id = ?{list_filter}
                    """,
                    parameters,
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
                item = _item_from_row(row)
                return ShoppingItem(
                    item.id,
                    item.chat_id,
                    item.name,
                    is_checked,
                    item.list_id,
                )

    def _remove_checked(
        self,
        chat_id: int,
        list_id: int | None,
    ) -> int:
        with closing(self._connect()) as connection:
            with connection:
                target_list_id = list_id or self._latest_list_id_in_connection(
                    connection,
                    chat_id,
                )
                if target_list_id is None:
                    return 0
                cursor = connection.execute(
                    """
                    DELETE FROM shopping_items
                    WHERE chat_id = ? AND list_id = ? AND is_checked = 1
                    """,
                    (chat_id, target_list_id),
                )
                return cursor.rowcount

    def _check_limit(self, names: list[str]) -> None:
        if len(names) > self._max_items_per_list:
            raise ListLimitReachedError(
                f"Shopping list is limited to "
                f"{self._max_items_per_list} items"
            )

    @staticmethod
    def _list_belongs_to_chat(
        connection: sqlite3.Connection,
        list_id: int,
        chat_id: int,
    ) -> bool:
        return connection.execute(
            "SELECT 1 FROM shopping_lists WHERE id = ? AND chat_id = ?",
            (list_id, chat_id),
        ).fetchone() is not None

    @staticmethod
    def _insert_items(
        connection: sqlite3.Connection,
        list_id: int,
        chat_id: int,
        names: list[str],
        created_by: int | None,
    ) -> list[ShoppingItem]:
        items = []
        for name in names:
            cursor = connection.execute(
                """
                INSERT INTO shopping_items (
                    list_id, chat_id, name, normalized_name, created_by
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (list_id, chat_id, name, name.casefold(), created_by),
            )
            items.append(
                ShoppingItem(
                    cursor.lastrowid,
                    chat_id,
                    name,
                    False,
                    list_id,
                )
            )
        return items


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
        list_id=row["list_id"],
    )
