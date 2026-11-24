"""Tests for non-sensitive configuration loading."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from source_adapter.cli import main
from source_adapter.config import (
    EXAMPLE_CONFIG_PATH,
    Config,
    config_from_mapping,
    load_config,
    with_overrides,
)

try:  # `python3 -m unittest tests.test_config`
    from tests.helpers import FakeClock  # noqa: F401  (keeps the shim symmetric)
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import FakeClock  # noqa: F401


def run_cli(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_yields_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "config.toml")
            self.assertEqual(config, Config())
            self.assertEqual(config.concurrency, 1)
            self.assertEqual(config.request_spacing_seconds, 2.0)

    def test_reads_a_written_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[run]\n"
                'cache_dir = "/tmp/cache-x"\n'
                "request_spacing_seconds = 1.5\n"
                "max_retries = 2\n"
                "tieba_page_size = 30\n"
                "aiotieba_include_nested = true\n",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.cache_dir, "/tmp/cache-x")
            self.assertEqual(config.request_spacing_seconds, 1.5)
            self.assertEqual(config.max_retries, 2)
            self.assertEqual(config.tieba_page_size, 30)
            self.assertTrue(config.aiotieba_include_nested)
            self.assertEqual(config.source_path, str(path))

    def test_shipped_example_config_is_valid(self):
        self.assertTrue(EXAMPLE_CONFIG_PATH.is_file())
        config = load_config(EXAMPLE_CONFIG_PATH)
        self.assertEqual(config.concurrency, 1)
        self.assertGreaterEqual(config.max_response_bytes, 1024)

    def test_relative_paths_resolve_against_the_package_root(self):
        config = Config(cache_dir=".cache")
        package_root = Path(__file__).resolve().parents[1]
        self.assertEqual(config.resolve(".cache"), package_root / ".cache")
        self.assertEqual(config.resolve("/tmp/absolute"), Path("/tmp/absolute"))


class ConfigValidationTests(unittest.TestCase):
    def test_rejects_unknown_sections_and_keys(self):
        with self.assertRaises(ValueError) as ctx:
            config_from_mapping({"nope": {"a": 1}})
        self.assertIn("sections", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            config_from_mapping({"run": {"mystery": 1}})
        self.assertIn("mystery", str(ctx.exception))

    def test_rejects_secret_like_keys(self):
        for key in ("api_key", "anysearch_token", "JUSTONEAPI_TOKEN_VALUE", "password"):
            with self.assertRaises(ValueError) as ctx:
                config_from_mapping({"run": {key: "whatever"}})
            self.assertIn("secret", str(ctx.exception))

    def test_rejects_endpoint_id_configuration(self):
        with self.assertRaises(ValueError) as ctx:
            config_from_mapping({"run": {"endpoint_id": "12345"}})
        self.assertIn("endpoint_id", str(ctx.exception))

    def test_rejects_out_of_range_values(self):
        with self.assertRaises(ValueError) as ctx:
            config_from_mapping({"run": {"concurrency": 3}})
        self.assertIn("concurrency", str(ctx.exception))
        with self.assertRaises(ValueError):
            config_from_mapping({"run": {"request_spacing_seconds": 5.0}})
        with self.assertRaises(ValueError):
            config_from_mapping({"run": {"max_retries": 99}})
        with self.assertRaises(ValueError):
            config_from_mapping({"run": {"tieba_page_size": 500}})

    def test_rejects_wrong_types(self):
        with self.assertRaises(ValueError) as ctx:
            config_from_mapping({"run": {"max_retries": "three"}})
        self.assertIn("integer", str(ctx.exception))
        with self.assertRaises(ValueError):
            config_from_mapping({"run": {"aiotieba_include_nested": "yes"}})

    def test_cli_overrides_win(self):
        config = Config(cache_dir="from-config")
        merged = with_overrides(config, cache_dir="from-flag", max_retries=None)
        self.assertEqual(merged.cache_dir, "from-flag")
        self.assertEqual(merged.max_retries, config.max_retries)


class ConfigCliTests(unittest.TestCase):
    def test_doctor_honours_the_configured_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "configured-cache"
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(f'[run]\ncache_dir = "{cache_dir}"\n', encoding="utf-8")
            code, _ = run_cli(["doctor", "--json", "--config", str(config_path)])
            self.assertEqual(code, 0)
            self.assertTrue(cache_dir.is_dir(), "配置中的 cache_dir 应被创建")

    def test_invalid_config_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("[run]\nconcurrency = 8\n", encoding="utf-8")
            code, output = run_cli(["doctor", "--json", "--config", str(config_path)])
            self.assertEqual(code, 2)
            self.assertIn("concurrency", output)


if __name__ == "__main__":
    unittest.main()
