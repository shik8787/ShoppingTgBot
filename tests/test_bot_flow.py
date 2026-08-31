from pathlib import Path
import tempfile
import unittest

from app import bot
from app.repository import ShoppingRepository


class _Message:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.reply_to_message = None

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _Update:
    def __init__(self, chat_id=100, user_id=1, text=""):
        self.effective_chat = type(
            "Chat",
            (),
            {"id": chat_id, "type": "private"},
        )()
        self.effective_user = type("User", (), {"id": user_id})()
        self.effective_message = _Message(text)


class _TelegramBot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


class _Context:
    def __init__(self, repository):
        self.application = type(
            "Application",
            (),
            {"bot_data": {"repository": repository}},
        )()
        self.bot = _TelegramBot()
        self.user_data = {}


class ShoppingBotFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        database = Path(self.temp_directory.name) / "shopping.db"
        self.repository = ShoppingRepository(str(database), 50)
        await self.repository.initialize()
        self.context = _Context(self.repository)
        self.update = _Update()

    async def asyncTearDown(self):
        self.temp_directory.cleanup()

    async def test_each_import_creates_an_independent_list(self):
        await bot._add_shopping_list(
            self.update,
            self.context,
            ["Молоко", "Хлеб"],
        )
        first_list_id = await self.repository.latest_list_id(100)

        await bot._add_shopping_list(
            self.update,
            self.context,
            ["Яблоки"],
        )
        second_list_id = await self.repository.latest_list_id(100)

        self.assertNotEqual(first_list_id, second_list_id)
        self.assertEqual(
            ["Молоко", "Хлеб"],
            [
                item.name
                for item in await self.repository.list_items(
                    100,
                    first_list_id,
                )
            ],
        )
        self.assertEqual(
            ["Яблоки"],
            [
                item.name
                for item in await self.repository.list_items(
                    100,
                    second_list_id,
                )
            ],
        )

    async def test_edit_replaces_only_the_selected_list_message(self):
        first = await self.repository.create_list(100, ["Молоко"], 1)
        second = await self.repository.create_list(100, ["Хлеб"], 1)

        await bot._replace_shopping_list(
            self.update,
            self.context,
            first.list_id,
            list_message_id=500,
            text="Список покупок:\n- Список покупок\n- Сыр\n- Кофе",
        )

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
        edit = self.context.bot.edits[0]
        self.assertEqual(500, edit["message_id"])
        callbacks = [
            button.callback_data
            for row in edit["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn(f"edit:{first.list_id}", callbacks)

    async def test_button_add_updates_original_list_message(self):
        shopping_list = await self.repository.create_list(
            100,
            ["Молоко"],
            1,
        )

        await bot._add_item(
            self.update,
            self.context,
            "Хлеб",
            shopping_list.list_id,
            list_message_id=700,
        )

        self.assertEqual(1, len(self.update.effective_message.replies))
        self.assertEqual(700, self.context.bot.edits[0]["message_id"])
        self.assertIn("Хлеб", self.context.bot.edits[0]["text"])


if __name__ == "__main__":
    unittest.main()
