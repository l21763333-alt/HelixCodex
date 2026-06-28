from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_flow
from codex_flow import (
    PredictionContractError,
    WorkflowManifest,
    _ensure_dirs,
    _needs_codegen_retry,
    _validate_t3_prediction_contract,
    execute_t3,
)
from config import get_paths


def _write_baseline_metrics(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "exp_00_baseline_package_detail.csv").write_text(
        "split,ds,true_pos_cnt,pred_pos_cnt,error_pos_cnt,abs_error_pos_cnt\n"
        "test,2026-01-01,10,7,-3,3\n",
        encoding="utf-8",
    )


def _write_standardized_actual(trial: Path) -> None:
    std = get_paths().trial_standardized_dir(trial)
    std.mkdir(parents=True, exist_ok=True)
    (std / "standardized_actual.csv").write_text(
        "source_table,split,ds,actual_value\npackage_detail,test,2026-01-01,10\n",
        encoding="utf-8",
    )


def _write_exec_plan(trial: Path, trial_id: str) -> None:
    agent2 = trial / "agent2"
    agent2.mkdir(parents=True, exist_ok=True)
    (agent2 / "agent2_execution_plan.yaml").write_text(
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


def _write_history_eval_exec_plan(trial: Path, trial_id: str) -> None:
    agent2 = trial / "agent2"
    agent2.mkdir(parents=True, exist_ok=True)
    (agent2 / "agent2_execution_plan.yaml").write_text(
        textwrap.dedent(
            f"""
            agent: Agent2
            trial_id: {trial_id}
            source_entrypoint: "src/lgb_package_to_dish_online_0319.py"
            train_command:
              - "python"
              - "candidate/code/train.py"
              - "--history_eval_only"
              - "--backtest_output_prefix"
              - "{trial_id}"
            output_contract:
              prediction_path: "{trial_id}_package_detail.csv"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_train_py(trial: Path, body: str) -> None:
    code_dir = get_paths().trial_code_dir(trial)
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "train.py").write_text(body, encoding="utf-8")


def _manifest(trial: Path, baseline: Path, trial_id: str) -> WorkflowManifest:
    return WorkflowManifest(
        trial_id=trial_id,
        experiment_dir=str(baseline),
        output_dir=str(trial),
        ask="test",
    )


def _execute_t3_quietly(manifest: WorkflowManifest, baseline_metric_dir: Path) -> dict:
    with contextlib.redirect_stdout(io.StringIO()):
        return execute_t3(manifest, baseline_metric_dir=str(baseline_metric_dir))


class T3RecoveryTest(unittest.TestCase):
    def test_valid_package_detail_output_computes_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trial_001"
            baseline = root / "baseline"
            trial_id = "trial_001"
            _ensure_dirs(str(trial))
            _write_baseline_metrics(baseline / "outputs")
            _write_standardized_actual(trial)
            _write_exec_plan(trial, trial_id)
            _write_train_py(
                trial,
                textwrap.dedent(
                    """
                    import argparse
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--output_dir", required=True)
                    parser.add_argument("--backtest_output_prefix", default="trial_001")
                    args, _ = parser.parse_known_args()
                    out = Path(args.output_dir)
                    out.mkdir(parents=True, exist_ok=True)
                    (out / f"{args.backtest_output_prefix}_package_detail.csv").write_text(
                        "split,ds,true_pos_cnt,pred_pos_cnt\\n"
                        "test,2026-01-01,10,8\\n",
                        encoding="utf-8",
                    )
                    """
                ),
            )

            comparison = _execute_t3_quietly(_manifest(trial, baseline, trial_id), baseline / "outputs")

            self.assertEqual(comparison["primary"]["new_wape"], 0.2)
            self.assertFalse(_needs_codegen_retry(comparison))
            self.assertTrue(json.loads((trial / "agent2" / "run_status.json").read_text())["eval_success"])

    def test_generic_output_contract_computes_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trial_generic"
            baseline = root / "baseline"
            trial_id = "trial_generic"
            _ensure_dirs(str(trial))
            (baseline / "outputs").mkdir(parents=True, exist_ok=True)
            (baseline / "outputs" / "baseline_generic.csv").write_text(
                "split,actual_value,predicted_value\n"
                "test,10,7\n",
                encoding="utf-8",
            )
            _write_standardized_actual(trial)
            (trial / "agent2").mkdir(parents=True, exist_ok=True)
            (trial / "agent2" / "agent2_execution_plan.yaml").write_text(
                textwrap.dedent(
                    f"""
                    agent: Agent2
                    trial_id: {trial_id}
                    train_command:
                      - "python"
                      - "candidate/code/train.py"
                      - "--output_dir"
                      - "{{trial_outputs_dir}}"
                    output_contract:
                      prediction_path: "generic_prediction.csv"
                      split_column: "split"
                      split_filter: "test"
                      actual_column: "actual_value"
                      prediction_column: "predicted_value"
                      baseline_prediction_globs: ["baseline_generic.csv"]
                      secondary_metric_globs: []
                      primary_level: "generic"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            _write_train_py(
                trial,
                textwrap.dedent(
                    """
                    import argparse
                    from pathlib import Path
                    parser = argparse.ArgumentParser()
                    parser.add_argument("--output_dir", required=True)
                    args, _ = parser.parse_known_args()
                    out = Path(args.output_dir)
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "generic_prediction.csv").write_text(
                        "split,actual_value,predicted_value\\n"
                        "test,10,8\\n",
                        encoding="utf-8",
                    )
                    """
                ),
            )

            comparison = _execute_t3_quietly(_manifest(trial, baseline, trial_id), baseline / "outputs")

            self.assertEqual(comparison["primary"]["level"], "generic")
            self.assertEqual(comparison["primary"]["new_wape"], 0.2)
            self.assertEqual(comparison["primary"]["old_wape"], 0.3)
            self.assertFalse(_needs_codegen_retry(comparison))

    def test_invalid_raw_feature_output_returns_retryable_metrics_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trial_002"
            baseline = root / "baseline"
            trial_id = "trial_002"
            _ensure_dirs(str(trial))
            _write_baseline_metrics(baseline / "outputs")
            _write_standardized_actual(trial)
            _write_exec_plan(trial, trial_id)
            _write_train_py(
                trial,
                textwrap.dedent(
                    """
                    import argparse
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--output_dir", required=True)
                    parser.add_argument("--backtest_output_prefix", default="trial_002")
                    args, _ = parser.parse_known_args()
                    out = Path(args.output_dir)
                    out.mkdir(parents=True, exist_ok=True)
                    (out / f"{args.backtest_output_prefix}_package_detail.csv").write_text(
                        "store_code,real_qty,prediction_value,actual_value\\n"
                        "1,10,8,10\\n",
                        encoding="utf-8",
                    )
                    """
                ),
            )

            comparison = _execute_t3_quietly(_manifest(trial, baseline, trial_id), baseline / "outputs")
            train_log = (trial / "logs" / "train.log").read_text(encoding="utf-8")
            new_metrics = json.loads((trial / "evaluation" / "new_metrics.json").read_text(encoding="utf-8"))
            run_status = json.loads((trial / "agent2" / "run_status.json").read_text(encoding="utf-8"))
            metric_comparison = json.loads((trial / "evaluation" / "metric_comparison.json").read_text(encoding="utf-8"))

            self.assertEqual(comparison["primary"]["new_wape"], 999.0)
            self.assertTrue(_needs_codegen_retry(comparison))
            self.assertFalse(run_status["eval_success"])
            self.assertEqual(run_status["failure_type"], "output_contract_error")
            self.assertEqual(metric_comparison["failure_type"], "output_contract_error")
            self.assertIn("T3 EVALUATION ERROR", train_log)
            self.assertIn("prediction contract invalid", new_metrics["error"])
            self.assertEqual(new_metrics["failure_type"], "output_contract_error")

    def test_contract_rejects_oversized_prediction_before_metric_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "trial_003_package_detail.csv"
            csv_path.write_text(
                "split,ds,true_pos_cnt,pred_pos_cnt\n"
                "test,2026-01-01,10,8\n",
                encoding="utf-8",
            )

            with patch.object(codex_flow, "MAX_T3_PREDICTION_BYTES", 10):
                with self.assertRaises(PredictionContractError) as ctx:
                    _validate_t3_prediction_contract(csv_path)

            self.assertIn("too large", str(ctx.exception))

    def test_calculate_metrics_timeout_becomes_retryable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trial_004"
            baseline = root / "baseline"
            trial_id = "trial_004"
            _ensure_dirs(str(trial))
            _write_baseline_metrics(baseline / "outputs")
            _write_standardized_actual(trial)
            _write_exec_plan(trial, trial_id)
            _write_train_py(
                trial,
                textwrap.dedent(
                    """
                    import argparse
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--output_dir", required=True)
                    parser.add_argument("--backtest_output_prefix", default="trial_004")
                    args, _ = parser.parse_known_args()
                    out = Path(args.output_dir)
                    out.mkdir(parents=True, exist_ok=True)
                    (out / f"{args.backtest_output_prefix}_package_detail.csv").write_text(
                        "split,ds,true_pos_cnt,pred_pos_cnt\\n"
                        "valid,2026-01-01,10,8\\n",
                        encoding="utf-8",
                    )
                    """
                ),
            )

            real_run = subprocess.run

            def fake_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and any(str(part).endswith("calculate_metrics.py") for part in cmd):
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)
                return real_run(cmd, *args, **kwargs)

            with patch("codex_flow.subprocess.run", side_effect=fake_run):
                comparison = _execute_t3_quietly(_manifest(trial, baseline, trial_id), baseline / "outputs")

            train_log = (trial / "logs" / "train.log").read_text(encoding="utf-8")
            new_metrics = json.loads((trial / "evaluation" / "new_metrics.json").read_text(encoding="utf-8"))
            run_status = json.loads((trial / "agent2" / "run_status.json").read_text(encoding="utf-8"))

            self.assertEqual(comparison["primary"]["new_wape"], 999.0)
            self.assertTrue(_needs_codegen_retry(comparison))
            self.assertEqual(run_status["failure_type"], "metric_timeout")
            self.assertIn("calculate_metrics.py timed out", train_log)
            self.assertIn("timed out", new_metrics["error"])
            self.assertEqual(new_metrics["failure_type"], "metric_timeout")

    def test_training_timeout_becomes_retryable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trial_005"
            baseline = root / "baseline"
            trial_id = "trial_005"
            _ensure_dirs(str(trial))
            _write_baseline_metrics(baseline / "outputs")
            _write_standardized_actual(trial)
            _write_exec_plan(trial, trial_id)
            _write_train_py(trial, "print('would run forever')\n")

            real_run = subprocess.run

            def fake_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and any(str(part).endswith("train.py") for part in cmd):
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=codex_flow.T3_TRAIN_TIMEOUT_SECONDS)
                return real_run(cmd, *args, **kwargs)

            with patch("codex_flow.subprocess.run", side_effect=fake_run):
                comparison = _execute_t3_quietly(_manifest(trial, baseline, trial_id), baseline / "outputs")

            train_log = (trial / "logs" / "train.log").read_text(encoding="utf-8")
            new_metrics = json.loads((trial / "evaluation" / "new_metrics.json").read_text(encoding="utf-8"))
            run_status = json.loads((trial / "agent2" / "run_status.json").read_text(encoding="utf-8"))
            metric_comparison = json.loads((trial / "evaluation" / "metric_comparison.json").read_text(encoding="utf-8"))

            self.assertEqual(comparison["primary"]["new_wape"], 999.0)
            self.assertTrue(_needs_codegen_retry(comparison))
            self.assertFalse(run_status["train_success"])
            self.assertFalse(run_status["eval_success"])
            self.assertEqual(run_status["failure_type"], "train_timeout")
            self.assertEqual(metric_comparison["failure_type"], "train_timeout")
            self.assertIn("T3 TRAINING ERROR", train_log)
            self.assertIn("training command timed out", new_metrics["error"])
            self.assertEqual(new_metrics["failure_type"], "train_timeout")

    def test_history_eval_contract_error_is_rejected_before_training(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "trial_006"
            baseline = root / "baseline"
            trial_id = "trial_006"
            _ensure_dirs(str(trial))
            _write_baseline_metrics(baseline / "outputs")
            _write_standardized_actual(trial)
            _write_history_eval_exec_plan(trial, trial_id)
            code_dir = get_paths().trial_code_dir(trial)
            code_dir.mkdir(parents=True, exist_ok=True)
            (code_dir / "train.py").write_text("print('must not run')\n", encoding="utf-8")
            (code_dir / "src").mkdir(parents=True, exist_ok=True)
            (code_dir / "src" / "lgb_package_to_dish_online_0319.py").write_text(
                textwrap.dedent(
                    """
                    def allocate_dish_prediction_by_target_date(*args, **kwargs):
                        return None

                    def run_t2_package_backtest(raw_df, args, output_dir):
                        valid_alloc_df = allocate_dish_prediction_by_target_date(raw_df, None, raw_df, args)
                        if args.history_eval_only:
                            return
                    """
                ),
                encoding="utf-8",
            )

            with patch("codex_flow.subprocess.run", side_effect=AssertionError("training should not run")):
                comparison = _execute_t3_quietly(_manifest(trial, baseline, trial_id), baseline / "outputs")

            train_log = (trial / "logs" / "train.log").read_text(encoding="utf-8")
            new_metrics = json.loads((trial / "evaluation" / "new_metrics.json").read_text(encoding="utf-8"))
            run_status = json.loads((trial / "agent2" / "run_status.json").read_text(encoding="utf-8"))
            metric_comparison = json.loads((trial / "evaluation" / "metric_comparison.json").read_text(encoding="utf-8"))

            self.assertEqual(comparison["primary"]["new_wape"], 999.0)
            self.assertTrue(_needs_codegen_retry(comparison))
            self.assertFalse(run_status["train_success"])
            self.assertFalse(run_status["eval_success"])
            self.assertEqual(run_status["failure_type"], "history_eval_contract_error")
            self.assertEqual(metric_comparison["failure_type"], "history_eval_contract_error")
            self.assertIn("T3 PREFLIGHT CONTRACT ERROR", train_log)
            self.assertIn("history_eval_only contract invalid", new_metrics["error"])
            self.assertEqual(new_metrics["failure_type"], "history_eval_contract_error")


if __name__ == "__main__":
    unittest.main()
