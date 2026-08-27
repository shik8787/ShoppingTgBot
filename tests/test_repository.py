from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
