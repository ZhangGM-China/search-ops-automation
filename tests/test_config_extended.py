import unittest
import os
import errno
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from src.config import get_env, get_brand_config, is_brand_enabled, reload_config, load_config
from unittest.mock import patch


class TestConfigExtended(unittest.TestCase):

    def test_get_env(self):
        with patch.dict(os.environ, {"ENV": "prod"}):
            self.assertEqual(get_env(), "prod")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENV", None)
            self.assertEqual(get_env(), "test")
        with patch.dict(os.environ, {"ENV": ""}):
            self.assertEqual(get_env(), "test")

    def test_get_brand_config(self):
        # atlas should exist
        config = get_brand_config("atlas")
        self.assertIn("table_name", config)

        # non-existent should fall back to atlas
        config2 = get_brand_config("non_existent")
        self.assertEqual(config2, config)

    def test_is_brand_enabled(self):
        import src.config as config_module

        with patch.object(
            config_module,
            "BRAND_CONFIGS",
            {"atlas": {"enabled": True}, "harbor": {"enabled": False}},
        ):
            self.assertTrue(is_brand_enabled("atlas"))
            self.assertFalse(is_brand_enabled("harbor"))
            self.assertFalse(is_brand_enabled("unknown"))

    def test_load_and_reload(self):
        # Should not crash
        config = load_config()
        self.assertIsNotNone(config)
        reload_config()

    def test_reload_config_is_safe_under_concurrent_calls(self):
        def worker():
            for _ in range(100):
                reload_config()

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker) for _ in range(8)]
            for future in as_completed(futures):
                future.result()

    def test_save_config_falls_back_to_in_place_write_when_replace_hits_bind_mount_error(self):
        import src.config as config_module

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("safety_config:\n  circuit_breaker_threshold: 10\n", encoding="utf-8")

            with patch.object(config_module, "CONFIG_PATH", config_path), \
                 patch("src.config.os.replace", side_effect=OSError(errno.EBUSY, "Device or resource busy")), \
                 patch.object(config_module, "reload_config") as mock_reload:
                config_module.save_config({"safety_config": {"circuit_breaker_threshold": 12}})

            self.assertIn("circuit_breaker_threshold: 12", config_path.read_text(encoding="utf-8"))
            self.assertFalse(config_path.with_suffix(".yaml.tmp").exists())
            mock_reload.assert_called_once()


if __name__ == '__main__':
    unittest.main()
