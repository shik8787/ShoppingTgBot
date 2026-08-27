import unittest

from app.bot import build_list_view, parse_shopping_list_message
from app.repository import ShoppingItem


class ShoppingListViewTest(unittest.TestCase):
    def test_renders_checkboxes_and_callbacks(self):
        items = [
            ShoppingItem(10, 100, "Молоко", False),
            ShoppingItem(11, 100, "Хлеб", True),
        ]

        text, keyboard = build_list_view(items)

        self.assertIn("⬜ Молоко", text)
        self.assertIn("✅ Хлеб", text)
        self.assertIn("Осталось: 1 из 2", text)
        self.assertEqual("toggle:10", keyboard.inline_keyboard[0][0].callback_data)
        self.assertEqual("toggle:11", keyboard.inline_keyboard[1][0].callback_data)

    def test_renders_empty_list_actions(self):
        text, keyboard = build_list_view([])

        self.assertIn("Список пуст", text)
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("add", callbacks)
        self.assertIn("refresh", callbacks)

    def test_parses_multiline_shopping_list(self):
        self.assertEqual(
            ["Молоко", "Хлеб", "Яблоки", "Сыр"],
            parse_shopping_list_message(
                "Список покупок:\n"
                "- Молоко\n"
                "• Хлеб\n"
                "3. Яблоки\n"
                "⬜ Сыр"
            ),
        )

    def test_only_matches_header_at_start(self):
        self.assertIsNone(
            parse_shopping_list_message("Покажи список покупок\nМолоко")
        )
        self.assertEqual([], parse_shopping_list_message("СПИСОК ПОКУПОК"))


if __name__ == "__main__":
    unittest.main()
