from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import lark_channel_bot


class LarkChannelBotTest(unittest.TestCase):
    def test_card_event_to_action_parses_button_and_form_value(self) -> None:
        event = types.SimpleNamespace(
            message_id="om_test",
            chat_id="oc_test",
            operator=types.SimpleNamespace(open_id="ou_user", user_id="u_user", name="User"),
            action=types.SimpleNamespace(value={"command": "/revise", "trial_id": "trial_001"}),
            raw={"event": {"action": {"form_value": {"suggestion": "调小校准强度"}}}},
        )

        parsed = lark_channel_bot.card_event_to_action(event)

        self.assertEqual(parsed["action"], "rollback")
        self.assertEqual(parsed["trial_id"], "trial_001")
        self.assertEqual(parsed["supplement"], "调小校准强度")
        self.assertEqual(parsed["source"], "channel_card")
        self.assertEqual(parsed["operator"]["open_id"], "ou_user")
        self.assertEqual(parsed["chat_id"], "oc_test")
        self.assertEqual(parsed["message_id"], "om_test")

    def test_message_event_to_action_parses_text_command(self) -> None:
        event = types.SimpleNamespace(
            message_id="om_text",
            chat_id="oc_test",
            sender=types.SimpleNamespace(open_id="ou_user", user_id=None, name=None),
            content_text="/branch holiday only; calibration off",
        )

        parsed = lark_channel_bot.message_event_to_action(event)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["action"], "rollback")
        self.assertIn("分支探索", parsed["supplement"])
        self.assertIn("holiday", parsed["supplement"])
        self.assertEqual(parsed["source"], "channel_message")

    def test_handle_card_action_writes_action_log(self) -> None:
        event = types.SimpleNamespace(
            message_id="om_test",
            chat_id="oc_test",
            operator=types.SimpleNamespace(open_id="ou_user"),
            action=types.SimpleNamespace(value=json.dumps({"command": "/keep", "trial_id": "trial_002"})),
            raw={},
        )

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "feishu_card_actions.jsonl"
            with patch.object(lark_channel_bot, "ACTION_LOG", log_path):
                parsed = lark_channel_bot.handle_card_action(event)

            lines = log_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(parsed["action"], "keep")
        self.assertEqual(len(lines), 1)
        saved = json.loads(lines[0])
        self.assertEqual(saved["trial_id"], "trial_002")
        self.assertEqual(saved["source"], "channel_card")
        self.assertIn("received_at", saved)


if __name__ == "__main__":
    unittest.main()
