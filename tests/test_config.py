import os
import unittest
from unittest.mock import patch

from app.config import get_settings


class ConfigTest(unittest.TestCase):
    def test_loads_defaults_and_allowed_chats(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "ALLOWED_CHAT_IDS": "123, -456",
            },
            clear=True,
        ):
            settings = get_settings()

        self.assertEqual("/data/shopping.db", settings.database_path)
        self.assertEqual(frozenset({123, -456}), settings.allowed_chat_ids)
        self.assertEqual(50, settings.max_items_per_list)

    def test_requires_token(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN"):
                get_settings()

    def test_rejects_excessive_item_limit(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "MAX_ITEMS_PER_LIST": "51",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "between 1 and 50"):
                get_settings()


if __name__ == "__main__":
    unittest.main()
