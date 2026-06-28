from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_flow import run_workflow
from config import get_paths


class FakeResult:
    def __init__(self, final_response: str = "ok") -> None:
        self.status = types.SimpleNamespace(value="completed")
        self.final_response = final_response
        self.usage = None
        self.error = None


class FakeThread:
    def __init__(self, codex: "FakeCodex", thread_id: str, phase: str, output_dir: Path) -> None:
        self.codex = codex
        self.id = thread_id
        self.phase = phase
        self.output_dir = output_dir

    def run(self, *_args, **_kwargs) -> FakeResult:
        self.codex.runs.append(self.phase)
        if self.phase == "evaluate":
            self.codex.write_evaluate_artifacts(self.output_dir)
        elif self.phase == "plan":
            self.codex.write_plan_artifacts(self.output_dir)
        elif self.phase == "codegen":
            self.codex.write_codegen_artifacts(self.output_dir, valid=False)
        elif self.phase == "codegen_retry":
            self.codex.codegen_retry_count += 1
            self.codex.write_codegen_artifacts(self.output_dir, valid=True)
        elif self.phase == "report":
            (self.output_dir / "final_report.md").write_text("# mock report\n", encoding="utf-8")
        elif self.phase == "feishu_card":
            (self.output_dir / "feishu_review_card.md").write_text("mock card\n", encoding="utf-8")
        return FakeResult(f"{self.phase} done")


class FakeCodex:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.started: list[str] = []
        self.runs: list[str] = []
        self.archived: list[str] = []
        self.codegen_retry_count = 0
        self.invalid_mode = "contract"
        self._phases = iter(["evaluate", "plan", "codegen", "report", "feishu_card"])
        self._thread_by_id: dict[str, str] = {}

    def thread_start(self, **_kwargs) -> FakeThread:
        phase = next(self._phases)
        thread_id = f"{phase}-thread"
        self.started.append(phase)
        self._thread_by_id[thread_id] = phase
        return FakeThread(self, thread_id, phase, self.output_dir)

    def thread_resume(self, thread_id: str) -> FakeThread:
        phase = self._thread_by_id.get(thread_id, "")
        if phase == "codegen":
            return FakeThread(self, thread_id, "codegen_retry", self.output_dir)
        return FakeThread(self, thread_id, phase, self.output_dir)

    def thread_archive(self, thread_id: str) -> None:
        self.archived.append(thread_id)

    def write_evaluate_artifacts(self, output_dir: Path) -> None:
        for sub in ("standardized", "agent1", "audit", "reports"):
            (output_dir / sub).mkdir(parents=True, exist_ok=True)
        (output_dir / "standardized" / "standardized_prediction.csv").write_text(
            "source_table,split,ds,prediction_value\npackage_detail,test,2026-01-01,8\n",
            encoding="utf-8",
        )
        (output_dir / "standardized" / "standardized_actual.csv").write_text(
            "source_table,split,ds,actual_value\npackage_detail,test,2026-01-01,10\n",
            encoding="utf-8",
        )
        (output_dir / "agent1" / "problem_context.json").write_text("{}", encoding="utf-8")
        (output_dir / "agent1" / "artifact_contract.json").write_text(
            json.dumps({
                "source_to_standard_mapping": [{
                    "source_table": "package_detail",
                    "prediction_value": "pred_pos_cnt",
                    "actual_value": "true_pos_cnt",
                }]
            }),
            encoding="utf-8",
        )
        for name in ("scan_result", "artifact_summary", "code_analysis", "log_summary"):
            (output_dir / "audit" / f"{name}.json").write_text("{}", encoding="utf-8")
        (output_dir / "agent1" / "badcase_diagnosis.md").write_text("mock\n", encoding="utf-8")
        (output_dir / "reports" / "optimization_suggestions.md").write_text("mock\n", encoding="utf-8")

    def write_plan_artifacts(self, output_dir: Path) -> None:
        (output_dir / "agent1").mkdir(parents=True, exist_ok=True)
        (output_dir / "reports").mkdir(parents=True, exist_ok=True)
        (output_dir / "agent1" / "feature_hypothesis.yaml").write_text("trial_id: mock\n", encoding="utf-8")
        (output_dir / "agent1" / "experiment_plan.yaml").write_text("changes: []\n", encoding="utf-8")
        (output_dir / "agent1" / "candidate_experiments.yaml").write_text("[]\n", encoding="utf-8")
        (output_dir / "reports" / "forecast_report.md").write_text("mock\n", encoding="utf-8")
        (output_dir / "reports" / "report_context.json").write_text("{}", encoding="utf-8")

    def write_codegen_artifacts(self, output_dir: Path, *, valid: bool) -> None:
        trial_id = output_dir.name
        code_dir = get_paths().trial_code_dir(output_dir)
        code_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "agent2").mkdir(parents=True, exist_ok=True)
        (output_dir / "agent2" / "agent2_execution_plan.yaml").write_text(
            textwrap.dedent(
                f"""
                agent: Agent2
                trial_id: {trial_id}
                train_command:
                  - "python"
                  - "candidate/code/train.py"
                  - "--backtest_output_prefix"
                  - "{trial_id}"
                output_contract:
                  prediction_path: "{trial_id}_package_detail.csv"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        if valid:
            body = """
                import argparse
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--output_dir", required=True)
                parser.add_argument("--backtest_output_prefix", default="trial")
                args, _ = parser.parse_known_args()
                out = Path(args.output_dir)
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{args.backtest_output_prefix}_package_detail.csv").write_text(
                    "split,ds,true_pos_cnt,pred_pos_cnt\\n"
                    "test,2026-01-01,10,8\\n",
                    encoding="utf-8",
                )
            """
        elif self.invalid_mode == "timeout":
            body = """
                # TRAIN_TIMEOUT_MARKER
                import argparse
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--output_dir", required=True)
                parser.add_argument("--backtest_output_prefix", default="trial")
                args, _ = parser.parse_known_args()
                Path(args.output_dir).mkdir(parents=True, exist_ok=True)
                print("simulated slow training")
            """
        else:
            body = """
                import argparse
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--output_dir", required=True)
                parser.add_argument("--backtest_output_prefix", default="trial")
                args, _ = parser.parse_known_args()
                out = Path(args.output_dir)
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{args.backtest_output_prefix}_package_detail.csv").write_text(
                    "store_code,real_qty,prediction_value,actual_value\\n"
                    "1,10,8,10\\n",
                    encoding="utf-8",
                )
            """
        (code_dir / "train.py").write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")


def _write_baseline(root: Path) -> Path:
    baseline = root / "baseline"
    (baseline / "outputs").mkdir(parents=True)
    (baseline / "src").mkdir()
    (baseline / "requirements.txt").write_text("pandas\n", encoding="utf-8")
    (baseline / "src" / "model.py").write_text("x = 1\n", encoding="utf-8")
    (baseline / "outputs" / "exp_00_baseline_package_detail.csv").write_text(
        "split,ds,true_pos_cnt,pred_pos_cnt,error_pos_cnt,abs_error_pos_cnt\n"
        "test,2026-01-01,10,7,-3,3\n",
        encoding="utf-8",
    )
    return baseline


class WorkflowMockCodexTest(unittest.TestCase):
    def test_workflow_retries_codegen_after_output_contract_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = _write_baseline(root)
            output_dir = root / "trial_001"
            fake_codex = FakeCodex(output_dir)

            with (
                patch("codex_flow._ensure_session", lambda _codex: None),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                manifest = run_workflow(
                    str(baseline),
                    "mock end-to-end retry",
                    str(output_dir),
                    codex=fake_codex,
                    resume=False,
                )

            comparison = json.loads((output_dir / "evaluation" / "metric_comparison.json").read_text(encoding="utf-8"))
            run_status = json.loads((output_dir / "agent2" / "run_status.json").read_text(encoding="utf-8"))
            stage_execute = (output_dir / "logs" / "stage_execute.log").read_text(encoding="utf-8")
            train_log = (output_dir / "logs" / "train.log").read_text(encoding="utf-8")

            self.assertEqual(fake_codex.codegen_retry_count, 1)
            self.assertIn("feedback failure to codegen retry=1", stage_execute)
            self.assertIn("T3 EVALUATION ERROR", train_log)
            self.assertEqual(run_status["eval_success"], True)
            self.assertIsNone(run_status["failure_type"])
            self.assertEqual(comparison["decision"], "keep")
            self.assertEqual(comparison["primary"]["new_wape"], 0.2)
            self.assertEqual(manifest.status("execute"), "completed")
            self.assertTrue((output_dir / "final_report.md").exists())
            self.assertTrue((output_dir / "feishu_review_card.md").exists())

    def test_workflow_retries_codegen_after_training_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = _write_baseline(root)
            output_dir = root / "trial_002"
            fake_codex = FakeCodex(output_dir)
            fake_codex.invalid_mode = "timeout"
            real_run = subprocess.run

            def fake_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and any(str(part).endswith("train.py") for part in cmd):
                    script = next(Path(str(part)) for part in cmd if str(part).endswith("train.py"))
                    if "TRAIN_TIMEOUT_MARKER" in script.read_text(encoding="utf-8"):
                        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
                return real_run(cmd, *args, **kwargs)

            with (
                patch("codex_flow._ensure_session", lambda _codex: None),
                patch("codex_flow.subprocess.run", side_effect=fake_run),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                manifest = run_workflow(
                    str(baseline),
                    "mock end-to-end training timeout retry",
                    str(output_dir),
                    codex=fake_codex,
                    resume=False,
                )

            comparison = json.loads((output_dir / "evaluation" / "metric_comparison.json").read_text(encoding="utf-8"))
            run_status = json.loads((output_dir / "agent2" / "run_status.json").read_text(encoding="utf-8"))
            stage_execute = (output_dir / "logs" / "stage_execute.log").read_text(encoding="utf-8")
            train_log = (output_dir / "logs" / "train.log").read_text(encoding="utf-8")

            self.assertEqual(fake_codex.codegen_retry_count, 1)
            self.assertIn("feedback failure to codegen retry=1", stage_execute)
            self.assertIn("T3 TRAINING ERROR", train_log)
            self.assertIn("training command timed out", train_log)
            self.assertEqual(run_status["eval_success"], True)
            self.assertIsNone(run_status["failure_type"])
            self.assertEqual(comparison["decision"], "keep")
            self.assertEqual(comparison["primary"]["new_wape"], 0.2)
            self.assertEqual(manifest.status("execute"), "completed")


if __name__ == "__main__":
    unittest.main()
