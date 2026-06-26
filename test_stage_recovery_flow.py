from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from lark_notify import parse_feishu_card_action, parse_feishu_command


class FakeStageInterrupted(RuntimeError):
    def __init__(self, trial_id: str, output_dir: str, phase: str, error: str):
        super().__init__(f"[{phase}] interrupted: {error}")
        self.trial_id = trial_id
        self.output_dir = output_dir
        self.phase = phase
        self.error = error


class FakeManifest:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.trial_id = Path(output_dir).name
        self.degraded: list[tuple[str, str]] = []

    def record_degraded(self, phase: str, error: str, artifacts: list[str]) -> None:
        self.degraded.append((phase, error))

    @classmethod
    def load(cls, output_dir: str):
        return cls(output_dir)


def _write_fake_trial_outputs(output_dir: str, decision: str = "keep") -> None:
    out = Path(output_dir)
    (out / "evaluation").mkdir(parents=True, exist_ok=True)
    (out / "agent2" / "code").mkdir(parents=True, exist_ok=True)
    (out / "agent2" / "code" / "train.py").write_text("print('ok')\n", encoding="utf-8")
    (out / "final_report.md").write_text("# fake report\n", encoding="utf-8")
    comparison = {
        "decision": decision,
        "wape_delta": 0.01,
        "bias_delta": 0.0,
        "primary": {
            "old_wape": 0.70,
            "new_wape": 0.69,
            "old_bias": 0.10,
            "new_bias": 0.10,
        },
        "secondary": {"old_wape": 0.4, "old_bias": 0.0},
    }
    (out / "evaluation" / "metric_comparison.json").write_text(
        json.dumps(comparison),
        encoding="utf-8",
    )


class StageRecoveryFlowTest(unittest.TestCase):
    def test_parse_recovery_commands(self) -> None:
        self.assertEqual(parse_feishu_command("/resume")["action"], "resume")
        retry = parse_feishu_command("/retry-stage report")
        self.assertEqual(retry["action"], "retry-stage")
        self.assertEqual(retry["phase"], "report")
        skip = parse_feishu_command("/skip-stage feishu_card")
        self.assertEqual(skip["action"], "skip-stage")
        self.assertEqual(skip["phase"], "feishu_card")
        self.assertEqual(parse_feishu_command("/stop")["action"], "stop")

    def test_card_action_round_trip(self) -> None:
        payload = {
            "action": {
                "value": {
                    "command": "/retry-stage report",
                    "trial_id": "trial_001",
                    "phase": "report",
                },
                "form_value": {"suggestion": ""},
            }
        }
        parsed = parse_feishu_card_action(payload)
        self.assertEqual(parsed["action"], "retry-stage")
        self.assertEqual(parsed["trial_id"], "trial_001")
        self.assertEqual(parsed["phase"], "report")

        with tempfile.TemporaryDirectory() as tmp:
            import lark_card_bot

            log_path = Path(tmp) / "actions.jsonl"
            with patch.object(lark_card_bot, "ACTION_LOG", log_path):
                status, data = lark_card_bot.handle_callback(payload)

            self.assertEqual(status, 200)
            self.assertIn("toast", data)
            logged = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(logged["trial_id"], "trial_001")
            self.assertEqual(logged["phase"], "report")
            self.assertEqual(logged["action"], "retry-stage")

    def test_lark_card_bot_verifies_url_challenge_token(self) -> None:
        import lark_card_bot

        fake_config = types.SimpleNamespace(
            feishu=types.SimpleNamespace(verification_token="secret-token"),
        )
        with patch.object(lark_card_bot, "get_config", lambda: fake_config):
            status, data = lark_card_bot.handle_callback({
                "type": "url_verification",
                "token": "secret-token",
                "challenge": "challenge-123",
            })
            self.assertEqual(status, 200)
            self.assertEqual(data, {"challenge": "challenge-123"})

            status, data = lark_card_bot.handle_callback({
                "type": "url_verification",
                "token": "wrong-token",
                "challenge": "challenge-123",
            })
            self.assertEqual(status, 403)
            self.assertIn("token", data["msg"])

    def test_lark_card_bot_sdk_handles_url_verification(self) -> None:
        import lark_card_bot

        if lark_card_bot.lark is None:
            self.skipTest("lark-oapi is not installed")

        fake_config = types.SimpleNamespace(
            feishu=types.SimpleNamespace(
                verification_token="secret-token",
                encrypt_key="",
            ),
        )
        body = json.dumps({
            "type": "url_verification",
            "token": "secret-token",
            "challenge": "sdk-challenge",
        }).encode("utf-8")

        old_cache = dict(lark_card_bot._sdk_card_handler_cache)
        try:
            lark_card_bot._sdk_card_handler_cache = {"key": None, "handler": None}
            with patch.object(lark_card_bot, "get_config", lambda: fake_config):
                handler = lark_card_bot._sdk_card_handler()
                response = handler.do(lark_card_bot._raw_request("/feishu/card", {}, body))
        finally:
            lark_card_bot._sdk_card_handler_cache = old_cache

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.content.decode("utf-8")),
            {"challenge": "sdk-challenge"},
        )

    def test_lark_card_bot_sdk_card_processor_writes_action_log(self) -> None:
        import lark_card_bot

        fake_card = types.SimpleNamespace(
            open_id="ou_test",
            user_id="user_test",
            open_message_id="om_test",
            open_chat_id="oc_test",
            action=types.SimpleNamespace(
                value={"command": "/keep", "trial_id": "trial_sdk"},
                form_value={"suggestion": ""},
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "actions.jsonl"
            with patch.object(lark_card_bot, "ACTION_LOG", log_path):
                result = lark_card_bot._process_sdk_card(fake_card)

            logged = json.loads(log_path.read_text(encoding="utf-8").strip())

        self.assertEqual(logged["action"], "keep")
        self.assertEqual(logged["trial_id"], "trial_sdk")
        self.assertEqual(logged["operator"]["open_id"], "ou_test")
        self.assertIn("toast", result)

    def test_card_server_writes_review_event_contract(self) -> None:
        import card_server
        import lark_notify
        from lark_notify import wait_for_review_event

        payload = {
            "token": "secret-token",
            "action": {
                "value": {"command": "/revise", "trial_id": "trial_007"},
                "form_value": {"suggestion": "调小校准强度"},
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "feishu_card_actions.jsonl"
            old_queue = card_server.DecisionQueue
            old_log = lark_notify.CARD_ACTION_LOG
            try:
                card_server.DecisionQueue = card_server._DecisionQueue(log_path)
                lark_notify.CARD_ACTION_LOG = log_path

                status, data = card_server.handle_callback(payload, "secret-token")
                self.assertEqual(status, 200)
                self.assertIn("toast", data)

                logged = json.loads(log_path.read_text(encoding="utf-8").strip())
                self.assertEqual(logged["action"], "rollback")
                self.assertEqual(logged["trial_id"], "trial_007")
                self.assertEqual(logged["supplement"], "调小校准强度")
                self.assertIn("received_at", logged)

                with patch("lark_notify.poll_recent_messages", return_value=[]):
                    event = wait_for_review_event(
                        "oc_test",
                        "trial_007",
                        timeout=0.2,
                        poll_interval=0.05,
                    )
            finally:
                card_server.DecisionQueue = old_queue
                lark_notify.CARD_ACTION_LOG = old_log

        self.assertIsNotNone(event)
        self.assertEqual(event["source"], "card")
        self.assertEqual(event["command"]["action"], "rollback")
        self.assertEqual(event["command"]["supplement"], "调小校准强度")

    def test_loop_recovers_interrupted_stage_without_training(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.mkdir()
            runs = root / "runs"
            calls: list[dict] = []

            def fake_run_workflow(**kwargs):
                calls.append(kwargs)
                output_dir = kwargs["output_dir"]
                if len(calls) == 1:
                    raise FakeStageInterrupted(Path(output_dir).name, output_dir, "report", "request timed out")
                _write_fake_trial_outputs(output_dir)
                return FakeManifest(output_dir)

            fake_flow = types.SimpleNamespace(
                run_workflow=fake_run_workflow,
                StageInterrupted=FakeStageInterrupted,
                WorkflowManifest=FakeManifest,
            )

            with patch.dict(sys.modules, {"codex_flow": fake_flow}):
                import loop

                with (
                    patch("builtins.print", lambda *_, **__: None),
                    patch.object(loop.AIExperimentLoop, "_git_mcp_enabled", lambda self: False),
                    patch.object(loop.AIExperimentLoop, "_lark_mcp_enabled", lambda self: False),
                    patch.object(loop, "human_review_enabled", lambda: True),
                    patch.object(loop, "notify_loop_start", lambda *_, **__: True),
                    patch.object(loop, "notify_loop_stop", lambda *_, **__: True),
                    patch.object(loop, "notify_error", lambda *_, **__: True),
                    patch.object(loop, "notify_command_result", lambda *_, **__: True),
                    patch.object(loop, "notify_stage_interrupted", lambda *_, **__: True),
                    patch.object(loop, "wait_for_recovery_event", lambda *_, **__: {"action": "resume", "phase": "report"}),
                    patch.object(loop, "feishu_review", lambda *_, **__: ("keep", None)),
                ):
                    agent_loop = loop.AIExperimentLoop(
                        experiment_dir=str(baseline),
                        ask="recovery dry-run",
                        output_base=str(runs),
                        max_iter=1,
                        human_review=True,
                    )
                    result = agent_loop.run()

            self.assertEqual(len(calls), 2)
            self.assertIsNone(calls[0]["resume_from_phase"])
            self.assertEqual(calls[1]["resume_from_phase"], "report")
            self.assertNotIn("异常", result["stop_reason"])
            self.assertTrue((runs / "trial_001" / ".checkpoint" / "snapshot.json").exists())

    def test_skip_degraded_report_continues_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.mkdir()
            runs = root / "runs"

            def fake_run_workflow(**kwargs):
                output_dir = kwargs["output_dir"]
                _write_fake_trial_outputs(output_dir)
                raise FakeStageInterrupted(Path(output_dir).name, output_dir, "report", "request timed out")

            def fake_fallback_report(output_dir: str, error: str) -> None:
                Path(output_dir, "final_report.md").write_text(
                    f"# fallback\n{error}\n",
                    encoding="utf-8",
                )

            fake_flow = types.SimpleNamespace(
                run_workflow=fake_run_workflow,
                StageInterrupted=FakeStageInterrupted,
                WorkflowManifest=FakeManifest,
                _write_fallback_report=fake_fallback_report,
            )

            with patch.dict(sys.modules, {"codex_flow": fake_flow}):
                import loop

                with (
                    patch("builtins.print", lambda *_, **__: None),
                    patch.object(loop.AIExperimentLoop, "_git_mcp_enabled", lambda self: False),
                    patch.object(loop.AIExperimentLoop, "_lark_mcp_enabled", lambda self: False),
                    patch.object(loop, "human_review_enabled", lambda: True),
                    patch.object(loop, "notify_loop_start", lambda *_, **__: True),
                    patch.object(loop, "notify_loop_stop", lambda *_, **__: True),
                    patch.object(loop, "notify_error", lambda *_, **__: True),
                    patch.object(loop, "notify_command_result", lambda *_, **__: True),
                    patch.object(loop, "notify_stage_interrupted", lambda *_, **__: True),
                    patch.object(loop, "wait_for_recovery_event", lambda *_, **__: {"action": "skip-stage", "phase": "report"}),
                    patch.object(loop, "feishu_review", lambda *_, **__: ("keep", None)),
                ):
                    agent_loop = loop.AIExperimentLoop(
                        experiment_dir=str(baseline),
                        ask="skip report dry-run",
                        output_base=str(runs),
                        max_iter=1,
                        human_review=True,
                    )
                    result = agent_loop.run()

            self.assertNotIn("异常", result["stop_reason"])
            self.assertTrue((runs / "trial_001" / "final_report.md").exists())
            self.assertTrue((runs / "trial_001" / ".checkpoint" / "snapshot.json").exists())

    def test_stop_from_recovery_card_stops_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.mkdir()
            runs = root / "runs"

            def fake_run_workflow(**kwargs):
                output_dir = kwargs["output_dir"]
                raise FakeStageInterrupted(Path(output_dir).name, output_dir, "report", "request timed out")

            fake_flow = types.SimpleNamespace(
                run_workflow=fake_run_workflow,
                StageInterrupted=FakeStageInterrupted,
                WorkflowManifest=FakeManifest,
            )

            with patch.dict(sys.modules, {"codex_flow": fake_flow}):
                import loop

                with (
                    patch("builtins.print", lambda *_, **__: None),
                    patch.object(loop.AIExperimentLoop, "_git_mcp_enabled", lambda self: False),
                    patch.object(loop.AIExperimentLoop, "_lark_mcp_enabled", lambda self: False),
                    patch.object(loop, "human_review_enabled", lambda: True),
                    patch.object(loop, "notify_loop_start", lambda *_, **__: True),
                    patch.object(loop, "notify_loop_stop", lambda *_, **__: True),
                    patch.object(loop, "notify_error", lambda *_, **__: True),
                    patch.object(loop, "notify_command_result", lambda *_, **__: True),
                    patch.object(loop, "notify_stage_interrupted", lambda *_, **__: True),
                    patch.object(loop, "wait_for_recovery_event", lambda *_, **__: {"action": "stop"}),
                ):
                    agent_loop = loop.AIExperimentLoop(
                        experiment_dir=str(baseline),
                        ask="stop recovery dry-run",
                        output_base=str(runs),
                        max_iter=1,
                        human_review=True,
                    )
                    result = agent_loop.run()

            self.assertEqual(result["stop_reason"], "用户停止阶段恢复")
            self.assertFalse((runs / "trial_001" / ".checkpoint").exists())


    def test_real_feishu_recovery_network_request(self) -> None:
        """Optional real Feishu integration test; no training or Codex SDK calls."""
        mode = os.environ.get("RUN_FEISHU_REAL_TEST", "").strip().lower()
        if mode not in {"1", "true", "send", "roundtrip"}:
            self.skipTest("set RUN_FEISHU_REAL_TEST=send or roundtrip to hit Feishu")

        from config import reload_config
        from lark_notify import notify_stage_interrupted, wait_for_recovery_event

        cfg = reload_config()
        missing = [
            name for name, value in {
                "FEISHU_APP_ID": cfg.feishu.app_id,
                "FEISHU_APP_SECRET": cfg.feishu.app_secret,
                "FEISHU_CHAT_ID": cfg.feishu.chat_id,
            }.items()
            if not value
        ]
        if missing:
            self.skipTest("missing Feishu config: " + ", ".join(missing))

        trial_id = os.environ.get("FEISHU_REAL_TEST_TRIAL_ID") or f"trial_real_{int(time.time())}"
        phase = os.environ.get("FEISHU_REAL_TEST_PHASE", "report")
        error = (
            "real Feishu recovery integration test: simulated Codex SDK request timed out; "
            "no training is started"
        )

        sent = notify_stage_interrupted(trial_id, phase, error)
        self.assertTrue(sent, "expected Feishu recovery card/post to be sent")

        if mode != "roundtrip":
            return

        timeout = int(os.environ.get("FEISHU_REAL_TEST_WAIT_SECONDS", "120"))
        cmd = wait_for_recovery_event(
            cfg.feishu.chat_id,
            trial_id,
            phase,
            timeout=timeout,
            poll_interval=max(1, int(cfg.feishu.poll_interval or 5)),
            sender_filter=cfg.loop.human_review.authorized_senders or None,
        )
        self.assertIsNotNone(
            cmd,
            f"no recovery command received in {timeout}s; reply /resume in Feishu or click the card",
        )
        self.assertIn(cmd.get("action"), {"resume", "retry-stage", "skip-stage", "stop", "status"})


if __name__ == "__main__":
    unittest.main()
