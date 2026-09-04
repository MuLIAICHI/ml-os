"""Tests for mlos.config — load, validate, and path resolution."""

import tempfile
import unittest
from pathlib import Path

from mlos.config import ConfigError, load_config

GOOD = """\
vault = "~/somewhere/vault"

[server]
host = "0.0.0.0"
port = 8787

[gates]
stale_threshold_minutes = 30

[[repos]]
name = "alpha"
path = "repo_alpha"

[session_manager]
kind = "cmux"
"""


class ConfigTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "config.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_load_ok(self):
        cfg = load_config(self._write(GOOD))
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertEqual(cfg.port, 8787)
        self.assertEqual(cfg.stale_threshold_minutes, 30)
        self.assertEqual(cfg.stale_threshold_seconds, 1800)
        self.assertEqual([r.name for r in cfg.repos], ["alpha"])
        self.assertEqual(cfg.session_manager["kind"], "cmux")

    def test_tilde_expansion(self):
        cfg = load_config(self._write(GOOD))
        self.assertTrue(cfg.vault.is_absolute())
        self.assertNotIn("~", str(cfg.vault))

    def test_relative_path_resolves_against_config_dir(self):
        p = self._write(GOOD)
        cfg = load_config(p)
        # "repo_alpha" is relative → resolved against the config file's directory.
        self.assertEqual(cfg.repos[0].path, p.parent / "repo_alpha")

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/no/such/config.toml")

    def test_missing_required_key(self):
        bad = GOOD.replace('stale_threshold_minutes = 30', "")
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_empty_repos_rejected(self):
        bad = GOOD.split("[[repos]]")[0] + '[session_manager]\nkind = "cmux"\n'
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_invalid_toml(self):
        with self.assertRaises(ConfigError):
            load_config(self._write("this is = = not toml ["))

    def test_notifier_defaults_when_absent(self):
        # A task-1-era config (no [notifier], no poll_interval) still loads with defaults.
        cfg = load_config(self._write(GOOD))
        self.assertEqual(cfg.poll_interval_seconds, 5)
        self.assertEqual(cfg.notifier.backend, "osascript")
        self.assertTrue(str(cfg.notifier.state_file).endswith("notified.json"))

    def test_notifier_bad_backend_rejected(self):
        bad = GOOD + '\n[notifier]\nbackend = "sms"\n'
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_notifier_open_url_parsed(self):
        cfg = load_config(self._write(
            GOOD + '\n[notifier]\nbackend = "terminal-notifier"\nopen_url = "http://localhost:8787"\n'
        ))
        self.assertEqual(cfg.notifier.open_url, "http://localhost:8787")

    def test_notifier_open_url_defaults_none(self):
        cfg = load_config(self._write(GOOD + '\n[notifier]\nbackend = "osascript"\n'))
        self.assertIsNone(cfg.notifier.open_url)

    def test_notifier_open_url_empty_rejected(self):
        bad = GOOD + '\n[notifier]\nopen_url = ""\n'
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_focus_command_default(self):
        cfg = load_config(self._write(GOOD))  # GOOD has [session_manager] kind only
        self.assertEqual(
            cfg.session_manager["focus_command"], ["cmux", "open", "{path}", "--focus", "true"]
        )

    def test_session_kind_bad_rejected(self):
        bad = GOOD.replace('kind = "cmux"', 'kind = "carrier-pigeon"')
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_focus_command_bad_type_rejected(self):
        # focus_command must be a list; a string is invalid.
        bad = GOOD.replace('[session_manager]\nkind = "cmux"',
                           '[session_manager]\nkind = "cmux"\nfocus_command = "not a list"')
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_state_file_outside_watched_repos(self):
        # R-T2-a (structural read-only): the notifier state file must not live inside any
        # watched repo. Check the real + fixture configs.
        for cfgpath in ["config.toml", "fixtures/config.test.toml"]:
            c = load_config(cfgpath)
            sf = str(c.notifier.state_file.resolve())
            for r in c.repos:
                self.assertFalse(
                    sf == str(r.path.resolve()) or sf.startswith(str(r.path.resolve()) + "/"),
                    f"state_file {sf} is inside watched repo {r.path}",
                )


if __name__ == "__main__":
    unittest.main()
