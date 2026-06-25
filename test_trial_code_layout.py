from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_flow import (
    _initial_code_source_dir,
    _write_data_refs,
    copy_source_to_trial,
    _normalize_trial_code_layout,
    _resolve_train_command,
    _write_fallback_feishu_card,
    _write_fallback_report,
)
from config import get_paths


class TrialCodeLayoutTest(unittest.TestCase):
    def test_legacy_root_code_is_moved_to_agent2_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "trial_001"
            legacy = trial / "code"
            canonical = trial / "candidate" / "code"
            legacy.mkdir(parents=True)
            canonical.mkdir(parents=True)
            (legacy / "train.py").write_text("print('ok')\n", encoding="utf-8")
            (legacy / "src").mkdir()
            (legacy / "src" / "model.py").write_text("x = 1\n", encoding="utf-8")

            moved = _normalize_trial_code_layout(str(trial), phase="test")

            self.assertIn("train.py", moved)
            self.assertIn("src", moved)
            self.assertTrue((canonical / "train.py").exists())
            self.assertTrue((canonical / "src" / "model.py").exists())
            self.assertFalse((legacy / "train.py").exists())

    def test_execution_plan_train_path_resolves_from_trial_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "trial_001"
            train_py = trial / "candidate" / "code" / "train.py"
            train_py.parent.mkdir(parents=True)
            train_py.write_text("print('ok')\n", encoding="utf-8")

            cmd = _resolve_train_command(
                ["python", "candidate/code/train.py", "--output_dir", "{output_dir}/outputs"],
                trial,
                "trial_001",
            )

            self.assertEqual(Path(cmd[1]), train_py.resolve())
            self.assertIn("--data_path", cmd)
            self.assertEqual(cmd[cmd.index("--data_path") + 1], str(get_paths().data_primary()))
            self.assertEqual(cmd[cmd.index("--output_dir") + 1], str(get_paths().trial_outputs_dir(trial)))

    def test_copy_source_does_not_copy_data_or_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "baseline"
            (source / "src").mkdir(parents=True)
            (source / "data").mkdir()
            (source / "outputs").mkdir()
            (source / "logs").mkdir()
            (source / "src" / "model.py").write_text("x = 1\n", encoding="utf-8")
            (source / "requirements.txt").write_text("pandas\n", encoding="utf-8")
            (source / "data" / "huge.csv").write_text("a\n1\n", encoding="utf-8")
            (source / "outputs" / "pred.csv").write_text("p\n1\n", encoding="utf-8")

            trial = root / "trial_001"
            copied = copy_source_to_trial(str(source), str(trial))
            code = trial / "candidate" / "code"

            self.assertIn("src/model.py", [item.replace("\\", "/") for item in copied])
            self.assertTrue((code / "src" / "model.py").exists())
            self.assertTrue((code / "requirements.txt").exists())
            self.assertFalse((code / "data").exists())
            self.assertFalse((code / "outputs").exists())
            self.assertFalse((code / "logs").exists())

    def test_initial_code_source_prefers_git_mcp_worktree_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_baseline = root / "Codex_flow" / "baseline"
            git_baseline = root / "ForecastModel_worktree" / "baseline"
            (local_baseline / "src").mkdir(parents=True)
            (git_baseline / "src").mkdir(parents=True)
            (local_baseline / "src" / "model.py").write_text("source = 'local'\n", encoding="utf-8")
            (git_baseline / "src" / "model.py").write_text("source = 'git'\n", encoding="utf-8")
            (git_baseline / "requirements.txt").write_text("lightgbm\n", encoding="utf-8")

            fake_config = types.SimpleNamespace(
                mcp=types.SimpleNamespace(
                    git=types.SimpleNamespace(
                        enabled=True,
                        scope="baseline_model",
                        repo_path="ForecastModel_worktree",
                        baseline_dir="baseline",
                    )
                )
            )

            with (
                patch("codex_flow.PROJECT_ROOT", root),
                patch("codex_flow.get_config", return_value=fake_config),
            ):
                source = _initial_code_source_dir(local_baseline)
                trial = root / "trial_001"
                copied = copy_source_to_trial(str(source), str(trial))

            self.assertEqual(source, git_baseline.resolve())
            self.assertIn("src/model.py", [item.replace("\\", "/") for item in copied])
            self.assertEqual(
                (trial / "candidate" / "code" / "src" / "model.py").read_text(encoding="utf-8"),
                "source = 'git'\n",
            )
            self.assertTrue((trial / "candidate" / "code" / "requirements.txt").exists())

    def test_data_refs_written_for_trial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "trial_001"
            refs = _write_data_refs(str(trial))

            self.assertFalse(refs["copied"])
            self.assertEqual(refs["mode"], "reference_only")
            self.assertTrue((trial / "inputs" / "data_refs.json").exists())

    def test_fallback_report_and_card_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "trial_001"
            (trial / "evaluation").mkdir(parents=True)
            (trial / "agent2").mkdir()
            (trial / "logs").mkdir()
            (trial / "evaluation" / "metric_comparison.json").write_text(
                '{"decision":"rollback","wape_delta":-0.1,"bias_delta":0.2,'
                '"primary":{"old_wape":0.5,"new_wape":0.6,"old_bias":0.1,"new_bias":0.3},'
                '"secondary":{"old_wape":0.4,"old_bias":0.0}}',
                encoding="utf-8",
            )
            (trial / "agent2" / "run_status.json").write_text(
                '{"train_success":true,"eval_success":true}',
                encoding="utf-8",
            )

            _write_fallback_report(str(trial), "request timed out")
            _write_fallback_feishu_card(str(trial), "request timed out")

            self.assertTrue((trial / "final_report.md").exists())
            self.assertTrue((trial / "feishu_review_card.md").exists())
            self.assertIn("ROLLBACK", (trial / "final_report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
