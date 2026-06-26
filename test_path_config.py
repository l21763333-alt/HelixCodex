from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config as config_module
from config import (
    Config,
    PathRegistry,
    build_codex_config,
    load_config,
    load_flow_paths,
    override_data_primary,
    reload_paths,
)


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
            self.assertEqual(registry.trial_code_dir("runs/001/trial_001").parts[-2:], ("candidate", "code"))

    def test_runtime_data_path_override_updates_primary_and_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "flow_paths.yaml"
            base.write_text(
                """
data:
  root: "baseline/data"
  primary: "baseline/data/base.csv"
""",
                encoding="utf-8",
            )

            try:
                reload_paths(base, root / "missing.local.yaml")
                registry = override_data_primary("/tmp/runtime/train.csv")

                self.assertEqual(registry.cfg.data.primary, "/tmp/runtime/train.csv")
                self.assertEqual(registry.cfg.data.root, "/tmp/runtime")
                self.assertEqual(str(registry.data_primary()), "/tmp/runtime/train.csv")
            finally:
                reload_paths()

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

    def test_codex_gateway_env_override_coerces_enabled_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "flow_config.yaml"
            cfg_path.write_text(
                """
codex_gateway:
  enabled: true
  hostname: "yaml-gateway.example.com"
""",
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"CODEX_GATEWAY_ENABLED": "false"},
                clear=True,
            ):
                cfg = load_config(cfg_path)

            self.assertIs(cfg.codex_gateway.enabled, False)
            self.assertEqual(cfg.codex_gateway.hostname, "yaml-gateway.example.com")

            with patch.dict(
                "os.environ",
                {
                    "CODEX_GATEWAY_ENABLED": "true",
                    "CODEX_GATEWAY_HOSTNAME": "env-gateway.example.com",
                },
                clear=True,
            ):
                cfg = load_config(cfg_path)

            self.assertIs(cfg.codex_gateway.enabled, True)
            self.assertEqual(cfg.codex_gateway.hostname, "env-gateway.example.com")

    def test_build_codex_config_sets_gateway_proxy_env(self) -> None:
        old_config = config_module._config
        try:
            cfg = Config()
            cfg.codex_gateway.enabled = True
            cfg.codex_gateway.listener = "127.0.0.1:18181"
            cfg.codex_gateway.proxy_url = ""
            config_module._config = cfg

            codex_cfg = build_codex_config()

            self.assertEqual(codex_cfg.env["HTTP_PROXY"], "http://127.0.0.1:18181")
            self.assertEqual(codex_cfg.env["HTTPS_PROXY"], "http://127.0.0.1:18181")
            self.assertEqual(codex_cfg.env["ALL_PROXY"], "http://127.0.0.1:18181")
            self.assertEqual(codex_cfg.env["NO_PROXY"], "127.0.0.1,localhost")
        finally:
            config_module._config = old_config

    def test_build_codex_config_can_reuse_existing_proxy_env(self) -> None:
        old_config = config_module._config
        try:
            cfg = Config()
            cfg.codex_gateway.enabled = True
            cfg.codex_gateway.mode = "proxy_env_only"
            cfg.codex_gateway.proxy_url = ""
            config_module._config = cfg

            with patch.dict(
                "os.environ",
                {"HTTPS_PROXY": "http://proxy.example:3128"},
                clear=True,
            ):
                codex_cfg = build_codex_config()

            self.assertEqual(codex_cfg.env["HTTPS_PROXY"], "http://proxy.example:3128")
            self.assertEqual(codex_cfg.env["HTTP_PROXY"], "http://proxy.example:3128")
        finally:
            config_module._config = old_config


    def test_proxy_env_only_ignores_default_local_listener_proxy(self) -> None:
        old_config = config_module._config
        try:
            cfg = Config()
            cfg.codex_gateway.enabled = True
            cfg.codex_gateway.mode = "proxy_env_only"
            cfg.codex_gateway.listener = "127.0.0.1:18080"
            cfg.codex_gateway.proxy_url = "http://127.0.0.1:18080"
            config_module._config = cfg

            with patch.dict(
                "os.environ",
                {"HTTPS_PROXY": "http://real-proxy.example:3128"},
                clear=True,
            ):
                codex_cfg = build_codex_config()

            self.assertEqual(codex_cfg.env["HTTPS_PROXY"], "http://real-proxy.example:3128")
        finally:
            config_module._config = old_config



if __name__ == "__main__":
    unittest.main()
