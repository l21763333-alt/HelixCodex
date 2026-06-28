from __future__ import annotations

import json
import subprocess
import threading
import time
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import lark_notify
from lark_notify import normalize_supplement, notify_command_result, wait_for_review_event
from mcp_servers.lark_research_server.server import feishu_review_via_mcp, parse_feedback
from config import GitMcpConfig, GitRepositoryConfig
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

    def test_send_prefers_sdk_when_available(self) -> None:
        with (
            patch("lark_notify._send_via_sdk", return_value=True) as sdk_send,
            patch("lark_notify._send_via_http", return_value=True) as http_send,
        ):
            self.assertTrue(lark_notify._send("interactive", "{}", "oc_test"))

        sdk_send.assert_called_once_with("interactive", "{}", "oc_test")
        http_send.assert_not_called()

    def test_send_falls_back_to_http_when_sdk_unavailable(self) -> None:
        with (
            patch("lark_notify._send_via_sdk", return_value=None) as sdk_send,
            patch("lark_notify._send_via_http", return_value=True) as http_send,
        ):
            self.assertTrue(lark_notify._send("interactive", "{}", "oc_test"))

        sdk_send.assert_called_once_with("interactive", "{}", "oc_test")
        http_send.assert_called_once_with("interactive", "{}", "oc_test")

    def test_sdk_send_builds_create_message_request(self) -> None:
        if lark_notify.CreateMessageRequest is None:
            self.skipTest("lark-oapi is not installed")

        captured = {}

        def create(request):
            captured["request"] = request
            return types.SimpleNamespace(success=lambda: True, code=0, msg="ok")

        fake_client = types.SimpleNamespace(
            im=types.SimpleNamespace(
                v1=types.SimpleNamespace(
                    message=types.SimpleNamespace(create=create),
                ),
            ),
        )
        with patch("lark_notify._get_sdk_client", return_value=fake_client):
            self.assertTrue(lark_notify._send_via_sdk("interactive", "{}", "oc_test"))

        request = captured["request"]
        self.assertEqual(request.receive_id_type, "chat_id")
        self.assertEqual(request.request_body.receive_id, "oc_test")
        self.assertEqual(request.request_body.msg_type, "interactive")
        self.assertEqual(request.request_body.content, "{}")

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
            trial_src = root / "runs" / "trial_001" / "candidate" / "code" / "src"
            baseline_src.mkdir(parents=True)
            trial_src.mkdir(parents=True)
            (baseline_src / "model.py").write_text("x = 1\n", encoding="utf-8")
            (trial_src / "model.py").write_text("x = 2\n", encoding="utf-8")
            (trial_src / "extra.py").write_text("y = 1\n", encoding="utf-8")

            git_cfg = GitMcpConfig(
                repo_path=str(root),
                baseline_dir="baseline",
                source_dir="baseline/src",
                requirements="baseline/requirements.txt",
                allowed_paths=["baseline/src/**", "baseline/requirements.txt"],
            )
            fake_config = types.SimpleNamespace(mcp=types.SimpleNamespace(git=git_cfg))

            with patch.object(git_mcp, "get_config", return_value=fake_config):
                diff = git_mcp.diff_trial_model_code(root / "runs" / "trial_001" / "candidate" / "code")

            self.assertEqual(diff["changed"], 1)
            self.assertEqual(diff["added"], 1)
            self.assertIn("M src/model.py", diff["summary"])
            self.assertIn("A src/extra.py", diff["summary"])

    def test_new_schema_maps_to_legacy_fields(self) -> None:
        git_cfg = GitMcpConfig(
            active_repo="supply",
            repositories={
                "supply": GitRepositoryConfig(
                    repo={
                        "path": "/tmp/worktree",
                        "url": "https://example.invalid/repo.git",
                        "lifecycle": "clone_if_missing",
                        "remote": "upstream",
                        "base_branch": "develop",
                    },
                    model={
                        "root": "pkg/model",
                        "copy_include": ["**"],
                        "copy_exclude": ["outputs/**"],
                        "publish_paths": ["pkg/model/**"],
                        "requirements_paths": [],
                        "entrypoint_candidates": ["train.py"],
                        "default_train_command": ["python", "{trial_code_dir}/train.py"],
                        "output_contract": {
                            "prediction_path": "pred.csv",
                            "actual_column": "actual",
                            "prediction_column": "prediction",
                        },
                    },
                    publish={
                        "mode": "branch_pr",
                        "branch_prefix": "exp/",
                        "target_branch": "develop",
                        "push_on_keep": True,
                        "create_pr": True,
                        "pr_draft": True,
                    },
                )
            },
        )

        repo = git_cfg.resolve_repo("supply")

        self.assertEqual(repo.repo_path, "/tmp/worktree")
        self.assertEqual(repo.repo_url, "https://example.invalid/repo.git")
        self.assertEqual(repo.repo_lifecycle, "clone_if_missing")
        self.assertEqual(repo.baseline_dir, "pkg/model")
        self.assertEqual(repo.allowed_paths, ["pkg/model/**"])
        self.assertEqual(repo.branch_prefix, "exp/")
        self.assertEqual(repo.push_target_branch, "develop")
        self.assertTrue(repo.create_pr_on_keep)
        self.assertEqual(repo.model.requirements_paths, [])
        self.assertEqual(repo.model.output_contract["prediction_path"], "pred.csv")

    def test_repo_id_selects_configured_model_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "repo_a"
            repo_b = root / "repo_b"
            (repo_a / "model" / "src").mkdir(parents=True)
            (repo_b / "custom" / "source").mkdir(parents=True)
            trial_src = root / "runs" / "trial_001" / "candidate" / "code" / "source"
            trial_src.mkdir(parents=True)
            (repo_b / "custom" / "source" / "model.py").write_text("x = 1\n", encoding="utf-8")
            (trial_src / "model.py").write_text("x = 2\n", encoding="utf-8")

            git_cfg = GitMcpConfig(
                active_repo="repo_b",
                repositories={
                    "repo_a": GitRepositoryConfig(
                        repo_path=str(repo_a),
                        baseline_dir="model",
                        source_dir="model/src",
                        requirements="model/requirements.txt",
                    ),
                    "repo_b": GitRepositoryConfig(
                        repo_path=str(repo_b),
                        baseline_dir="custom",
                        source_dir="custom/source",
                        requirements="custom/req.txt",
                        allowed_paths=["custom/source/**", "custom/req.txt"],
                        branch_prefix="b/",
                        remote="upstream",
                        base_branch="prod",
                    ),
                },
            )
            fake_config = types.SimpleNamespace(mcp=types.SimpleNamespace(git=git_cfg))

            with patch.object(git_mcp, "get_config", return_value=fake_config):
                ctx = git_mcp._repo_context("repo_b")
                diff = git_mcp.diff_trial_model_code(root / "runs" / "trial_001" / "candidate" / "code", repo_id="repo_b")

            self.assertEqual(ctx.repo_id, "repo_b")
            self.assertEqual(ctx.repo_path, repo_b.resolve())
            self.assertEqual(ctx.source_dir, (repo_b / "custom" / "source").resolve())
            self.assertEqual(ctx.allowed_pathspecs, ["custom/source", "custom/req.txt"])
            self.assertEqual(diff["changed"], 1)
            self.assertIn("M source/model.py", diff["summary"])
            self.assertEqual(diff["repo_id"], "repo_b")

    def test_clone_if_missing_uses_local_bare_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin.git"
            seed = root / "seed"
            clone_path = root / "clone"
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=seed, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=seed, check=True)
            (seed / "model").mkdir()
            (seed / "model" / "train.py").write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "model/train.py"], cwd=seed, check=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=seed, check=True, capture_output=True)
            subprocess.run(["git", "branch", "-M", "develop"], cwd=seed, check=True)
            subprocess.run(["git", "push", "origin", "develop"], cwd=seed, check=True, capture_output=True)

            git_cfg = GitMcpConfig(
                active_repo="clone",
                repositories={
                    "clone": GitRepositoryConfig(
                        repo={
                            "path": str(clone_path),
                            "url": str(origin),
                            "lifecycle": "clone_if_missing",
                            "remote": "origin",
                            "base_branch": "develop",
                        },
                        model={
                            "root": "model",
                            "copy_include": ["**"],
                            "publish_paths": ["model/**"],
                            "requirements_paths": [],
                        },
                        publish={"branch_prefix": "exp/", "target_branch": "develop"},
                    )
                },
            )
            fake_config = types.SimpleNamespace(mcp=types.SimpleNamespace(git=git_cfg))

            with patch.object(git_mcp, "get_config", return_value=fake_config):
                sync = git_mcp.sync_remote_base(repo_id="clone")

            self.assertTrue(sync["synced"])
            self.assertTrue((clone_path / ".git").exists())
            self.assertTrue((clone_path / "model" / "train.py").exists())


if __name__ == "__main__":
    unittest.main()
