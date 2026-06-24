from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import PathRegistry, load_config, load_flow_paths


class PathConfigTest(unittest.TestCase):
    def test_path_config_loads_and_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "flow_paths.yaml"
            local = root / "flow_paths.local.yaml"
            base.write_text(
                """
data:
  primary: "baseline/data/base.csv"
trial:
  code_dir: "candidate/code"
global_artifacts:
  git_action_log: "runs/git.jsonl"
""",
                encoding="utf-8",
            )
            local.write_text(
                """
data:
  primary: "D:/local/data.csv"
trial:
  outputs_dir: "outputs/test_outputs"
""",
                encoding="utf-8",
            )

            registry = PathRegistry(load_flow_paths(base, local))

            self.assertEqual(registry.cfg.data.primary, "D:/local/data.csv")
            self.assertEqual(registry.cfg.trial.code_dir, "candidate/code")
            self.assertEqual(registry.cfg.trial.outputs_dir, "outputs/test_outputs")
            self.assertTrue(str(registry.trial_code_dir("runs/001/trial_001")).endswith("candidate\\code"))

    def test_feishu_credentials_only_load_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "flow_config.yaml"
            cfg_path.write_text(
                """
feishu:
  enabled: true
  app_id: "yaml_app_id"
  app_secret: "yaml_secret"
  chat_id: "yaml_chat"
  verification_token: "yaml_token"
  poll_interval: 7
""",
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "FEISHU_APP_ID": "env_app_id",
                    "FEISHU_APP_SECRET": "env_secret",
                    "FEISHU_CHAT_ID": "env_chat",
                    "FEISHU_VERIFICATION_TOKEN": "env_token",
                },
                clear=False,
            ):
                cfg = load_config(cfg_path)

            self.assertTrue(cfg.feishu.enabled)
            self.assertEqual(cfg.feishu.poll_interval, 7)
            self.assertEqual(cfg.feishu.app_id, "env_app_id")
            self.assertEqual(cfg.feishu.app_secret, "env_secret")
            self.assertEqual(cfg.feishu.chat_id, "env_chat")
            self.assertEqual(cfg.feishu.verification_token, "env_token")


if __name__ == "__main__":
    unittest.main()
