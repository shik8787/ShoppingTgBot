from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from app.repository import ListLimitReachedError, ShoppingRepository


class ShoppingRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        database = Path(self.temp_directory.name) / "shopping.db"
        self.repository = ShoppingRepository(str(database), max_items_per_list=2)
        await self.repository.initialize()

    async def asyncTearDown(self):
        self.temp_directory.cleanup()

    async def test_adds_toggles_and_removes_checked_items(self):
        added = await self.repository.add_item(100, "  Молоко  ", 1)
        self.assertTrue(added.created)
        self.assertEqual("Молоко", added.item.name)

        toggled = await self.repository.toggle_item(100, added.item.id)
        self.assertTrue(toggled.is_checked)

        duplicate = await self.repository.add_item(100, "молоко", 2)
        self.assertFalse(duplicate.created)
        self.assertTrue(duplicate.reactivated)
        self.assertFalse(duplicate.item.is_checked)

        await self.repository.toggle_item(100, added.item.id)
        self.assertEqual(1, await self.repository.remove_checked(100))
        self.assertEqual([], await self.repository.list_items(100))

    async def test_keeps_chat_lists_isolated(self):
        first = await self.repository.add_item(100, "Хлеб", 1)
        await self.repository.add_item(200, "Яблоки", 2)

        self.assertIsNone(await self.repository.toggle_item(200, first.item.id))
        self.assertEqual(["Хлеб"], [
            item.name for item in await self.repository.list_items(100)
        ])
        self.assertEqual(["Яблоки"], [
            item.name for item in await self.repository.list_items(200)
        ])

    async def test_enforces_per_chat_item_limit(self):
        await self.repository.add_item(100, "Хлеб", 1)
        await self.repository.add_item(100, "Молоко", 1)

        with self.assertRaises(ListLimitReachedError):
            await self.repository.add_item(100, "Сыр", 1)

        await self.repository.add_item(200, "Сыр", 1)

    async def test_creates_independent_lists_in_same_chat(self):
        first = await self.repository.create_list(
            100,
            ["Молоко", "Хлеб"],
            1,
        )
        second = await self.repository.create_list(
            100,
            ["Яблоки"],
            2,
        )

        self.assertNotEqual(first.list_id, second.list_id)
        self.assertEqual(
            ["Молоко", "Хлеб"],
            [
                item.name
                for item in await self.repository.list_items(
                    100,
                    first.list_id,
                )
            ],
        )
        self.assertEqual(
            ["Яблоки"],
            [
                item.name
                for item in await self.repository.list_items(
                    100,
                    second.list_id,
                )
            ],
        )
        self.assertEqual(second.list_id, await self.repository.latest_list_id(100))

    async def test_replaces_only_selected_list(self):
        first = await self.repository.create_list(100, ["Молоко"], 1)
        second = await self.repository.create_list(100, ["Хлеб"], 1)

        replaced = await self.repository.replace_list(
            100,
            first.list_id,
            ["Сыр", "Сыр", "Кофе"],
            2,
        )

        self.assertEqual(1, replaced.duplicate_count)
        self.assertEqual(
            ["Сыр", "Кофе"],
            [
                item.name
                for item in await self.repository.list_items(
                    100,
                    first.list_id,
                )
            ],
        )
        self.assertEqual(
            ["Хлеб"],
            [
                item.name
                for item in await self.repository.list_items(
                    100,
                    second.list_id,
                )
            ],
        )

    async def test_migrates_existing_chat_items_to_first_list(self):
        self.temp_directory.cleanup()
        self.temp_directory = tempfile.TemporaryDirectory()
        database = Path(self.temp_directory.name) / "legacy.db"
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE shopping_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        normalized_name TEXT NOT NULL,
                        is_checked INTEGER NOT NULL DEFAULT 0,
                        created_by INTEGER,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (chat_id, normalized_name)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO shopping_items (
                        chat_id, name, normalized_name, created_by
                    ) VALUES (100, 'Молоко', 'молоко', 1)
                    """
                )

        repository = ShoppingRepository(str(database), max_items_per_list=2)
        await repository.initialize()
        list_id = await repository.latest_list_id(100)

        self.assertIsNotNone(list_id)
        self.assertEqual(
            ["Молоко"],
            [item.name for item in await repository.list_items(100, list_id)],
        )


if __name__ == "__main__":
    unittest.main()
