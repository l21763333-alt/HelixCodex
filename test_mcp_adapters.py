from __future__ import annotations

import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lark_notify
from lark_notify import normalize_supplement, notify_command_result, wait_for_review_event
from mcp_servers.lark_research_server.server import feishu_review_via_mcp, parse_feedback
from mcp_servers.git_research_server import baseline_git_server as git_mcp


class LarkMcpAdapterTest(unittest.TestCase):
    def test_parse_revise_feedback(self) -> None:
        feedback = parse_feedback("/revise reduce calibration", "trial_001")
        self.assertEqual(feedback["trial_id"], "trial_001")
        self.assertEqual(feedback["decision"], "rollback")
        self.assertEqual(feedback["supplement"], "reduce calibration")

    def test_parse_empty_revise_feedback_is_rollback(self) -> None:
        feedback = parse_feedback("/revise", "trial_001")
        self.assertEqual(feedback["decision"], "rollback")
        self.assertIsNone(feedback["supplement"])

    def test_parse_branch_feedback(self) -> None:
        feedback = parse_feedback("/branch holiday only; calibration off", "trial_002")
        self.assertEqual(feedback["decision"], "branch")
        self.assertIn("holiday", feedback["supplement"])

    def test_notify_command_result_ignores_raw_dict_supplement(self) -> None:
        with patch("lark_notify.send_markdown") as send:
            send.return_value = True
            self.assertTrue(notify_command_result("keep", {"raw": {"source": "card"}}, 2))

        sent = send.call_args.args[0]
        self.assertNotIn("raw", sent)
        self.assertNotIn("补充已注入", sent)

    def test_mcp_review_does_not_return_raw_event_as_supplement(self) -> None:
        with (
            patch("mcp_servers.lark_research_server.server.send_experiment_review"),
            patch("mcp_servers.lark_research_server.server.send_status_update"),
            patch("mcp_servers.lark_research_server.server.wait_human_feedback") as wait,
        ):
            wait.return_value = {
                "trial_id": "trial_001",
                "decision": "keep",
                "source": "card",
                "raw": {"source": "card", "command": {"action": "keep"}},
            }
            decision, supplement = feishu_review_via_mcp("trial_001", "ask", {}, 1, "keep")

        self.assertEqual(decision, "keep")
        self.assertIsNone(supplement)

    def test_wait_for_review_event_uses_fixed_card_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_log = lark_notify.CARD_ACTION_LOG
            lark_notify.CARD_ACTION_LOG = Path(tmp) / "feishu_card_actions.jsonl"
            try:
                def append_action() -> None:
                    time.sleep(0.1)
                    lark_notify.CARD_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
                    with lark_notify.CARD_ACTION_LOG.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "received_at": time.time(),
                            "trial_id": "trial_001",
                            "action": "keep",
                            "supplement": None,
                        }) + "\n")

                thread = threading.Thread(target=append_action)
                thread.start()
                with patch("lark_notify.poll_recent_messages", return_value=[]):
                    event = wait_for_review_event("oc_test", "trial_001", timeout=2.5, poll_interval=1.2)
                thread.join()
            finally:
                lark_notify.CARD_ACTION_LOG = old_log

        self.assertIsNotNone(event)
        self.assertEqual(event["source"], "card")
        self.assertEqual(event["command"]["action"], "keep")

    def test_normalize_supplement_extracts_text_only(self) -> None:
        self.assertEqual(normalize_supplement({"supplement": " tune feature "}), "tune feature")
        self.assertIsNone(normalize_supplement({"raw": {"source": "card"}}))


class GitMcpAdapterTest(unittest.TestCase):
    def test_diff_trial_model_code_is_baseline_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_src = root / "baseline" / "src"
            trial_src = root / "runs" / "trial_001" / "agent2" / "code" / "src"
            baseline_src.mkdir(parents=True)
            trial_src.mkdir(parents=True)
            (baseline_src / "model.py").write_text("x = 1\n", encoding="utf-8")
            (trial_src / "model.py").write_text("x = 2\n", encoding="utf-8")
            (trial_src / "extra.py").write_text("y = 1\n", encoding="utf-8")

            old_baseline_src = git_mcp._baseline_src
            old_baseline_req = git_mcp._baseline_requirements
            try:
                git_mcp._baseline_src = lambda: baseline_src  # type: ignore[assignment]
                git_mcp._baseline_requirements = lambda: root / "baseline" / "requirements.txt"  # type: ignore[assignment]
                diff = git_mcp.diff_trial_model_code(root / "runs" / "trial_001" / "agent2" / "code")
            finally:
                git_mcp._baseline_src = old_baseline_src  # type: ignore[assignment]
                git_mcp._baseline_requirements = old_baseline_req  # type: ignore[assignment]

            self.assertEqual(diff["changed"], 1)
            self.assertEqual(diff["added"], 1)
            self.assertIn("M src/model.py", diff["summary"])
            self.assertIn("A src/extra.py", diff["summary"])


if __name__ == "__main__":
    unittest.main()
