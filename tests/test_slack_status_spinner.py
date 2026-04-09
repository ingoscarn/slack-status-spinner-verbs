import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slack_sdk.errors import SlackApiError

from slack_status_spinner import (
    DEFAULT_EXPIRATION_PADDING_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_STATUS_EMOJI,
    DEFAULT_STATUS_SUFFIX,
    MAX_CONSECUTIVE_EMOJI_ERRORS,
    Config,
    SavedStatus,
    SlackStatusSpinner,
    load_config,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
ENV_PATH = REPO_ROOT / ".env"
WRAPPER_PATH = REPO_ROOT / "slack_spinner"
VERBS_PATH = REPO_ROOT / "spinner_verbs.txt"

EXPECTED_ENV_KEYS = [
    "SLACK_USER_TOKEN",
    "STATUS_EMOJI",
    "ENABLE_DYNAMIC_EMOJIS",
    "EMOJIS_FILE",
    "STATUS_PREFIX",
    "STATUS_SUFFIX",
    "UPDATE_INTERVAL_SECONDS",
    "STATUS_EXPIRATION_SECONDS",
    "VERB_ORDER",
    "VERBS_FILE",
]


def parse_env_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0])
    return keys


class FakeWebClient:
    def __init__(self, invalid_emojis: set[str] | None = None):
        self.get_calls = 0
        self.set_calls = []
        self.attempted_profiles = []
        self.invalid_emojis = invalid_emojis or set()
        self.profile = {
            "status_text": "Existing status",
            "status_emoji": ":wave:",
            "status_expiration": 0,
        }

    def users_profile_get(self):
        self.get_calls += 1
        return {"profile": self.profile}

    def users_profile_set(self, profile):
        self.attempted_profiles.append(profile)
        if profile["status_emoji"] in self.invalid_emojis:
            raise SlackApiError(
                message="invalid emoji",
                response=FakeSlackResponse("profile_status_set_failed_not_valid_emoji"),
            )
        self.set_calls.append(profile)
        return {"ok": True}


class FakeSlackResponse:
    def __init__(self, error: str, status_code: int = 200, headers: dict[str, str] | None = None):
        self.data = {"error": error}
        self.status_code = status_code
        self.headers = headers or {}

    def get(self, key, default=None):
        return self.data.get(key, default)


class SlackStatusSpinnerRuntimeTests(unittest.TestCase):
    def make_config(self, verbs_file: Path) -> Config:
        return Config(
            slack_user_token="xoxp-test-token",
            verbs_file=verbs_file,
            status_emoji=":thought_balloon:",
            dynamic_emojis_enabled=False,
            emojis_file=None,
            status_prefix="",
            status_suffix="...",
            interval_seconds=10,
            expiration_seconds=40,
            verb_order="rotate",
        )

    def test_load_verbs_ignores_blank_lines_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\n\n# disabled\nAnalyzing\n", encoding="utf-8")

            spinner = SlackStatusSpinner(self.make_config(verbs_file))

            self.assertEqual(spinner._load_verbs(), ["Thinking", "Analyzing"])

    def test_run_updates_status_and_restores_original(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\nAnalyzing\n", encoding="utf-8")

            spinner = SlackStatusSpinner(self.make_config(verbs_file))
            fake_client = FakeWebClient()
            spinner.client = fake_client
            spinner._sleep_with_shutdown = lambda _seconds: False

            exit_code = spinner.run()

            self.assertEqual(exit_code, 0)
            self.assertEqual(fake_client.get_calls, 1)
            self.assertEqual(len(fake_client.set_calls), 2)
            self.assertEqual(fake_client.set_calls[0]["status_text"], "Thinking...")
            self.assertEqual(fake_client.set_calls[0]["status_emoji"], ":thought_balloon:")
            self.assertEqual(fake_client.set_calls[1]["status_text"], "Existing status")
            self.assertEqual(fake_client.set_calls[1]["status_emoji"], ":wave:")

    def test_restore_clears_already_expired_original_status(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\n", encoding="utf-8")

            spinner = SlackStatusSpinner(self.make_config(verbs_file))
            fake_client = FakeWebClient()
            spinner.client = fake_client
            spinner.original_status = SavedStatus(
                status_text="Old",
                status_emoji=":zzz:",
                status_expiration=1,
            )

            spinner._restore_previous_status()

            self.assertEqual(fake_client.set_calls[-1]["status_text"], "")
            self.assertEqual(fake_client.set_calls[-1]["status_emoji"], "")
            self.assertEqual(fake_client.set_calls[-1]["status_expiration"], 0)

    def test_empty_verbs_file_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("\n# comment only\n", encoding="utf-8")

            spinner = SlackStatusSpinner(self.make_config(verbs_file))

            with self.assertRaises(RuntimeError):
                spinner._load_verbs()

    def test_load_emojis_ignores_blank_lines_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\n", encoding="utf-8")
            emojis_file = Path(tmp_dir) / "emojis.txt"
            emojis_file.write_text(":robot_face:\n\n# disabled\n🐝\n", encoding="utf-8")

            config = self.make_config(verbs_file)
            config.dynamic_emojis_enabled = True
            config.emojis_file = emojis_file
            spinner = SlackStatusSpinner(config)

            self.assertEqual(spinner._load_emojis(), [":robot_face:", "🐝"])

    def test_run_uses_dynamic_emojis_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\nAnalyzing\n", encoding="utf-8")
            emojis_file = Path(tmp_dir) / "emojis.txt"
            emojis_file.write_text(":robot_face:\n:pepebrain:\n", encoding="utf-8")

            config = self.make_config(verbs_file)
            config.dynamic_emojis_enabled = True
            config.emojis_file = emojis_file
            spinner = SlackStatusSpinner(config)
            fake_client = FakeWebClient()
            spinner.client = fake_client
            spinner._sleep_with_shutdown = lambda _seconds: False

            exit_code = spinner.run()

            self.assertEqual(exit_code, 0)
            self.assertEqual(fake_client.set_calls[0]["status_emoji"], ":robot_face:")

    def test_invalid_dynamic_emoji_is_removed_and_next_emoji_is_used(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\n", encoding="utf-8")
            emojis_file = Path(tmp_dir) / "emojis.txt"
            emojis_file.write_text(":bad:\n:good:\n", encoding="utf-8")

            config = self.make_config(verbs_file)
            config.dynamic_emojis_enabled = True
            config.emojis_file = emojis_file
            spinner = SlackStatusSpinner(config)
            fake_client = FakeWebClient(invalid_emojis={":bad:"})
            spinner.client = fake_client
            spinner._sleep_with_shutdown = lambda _seconds: False

            exit_code = spinner.run()

            self.assertEqual(exit_code, 0)
            self.assertEqual(fake_client.attempted_profiles[0]["status_emoji"], ":bad:")
            self.assertEqual(fake_client.set_calls[0]["status_emoji"], ":good:")
            self.assertEqual(spinner.available_dynamic_emojis, [":good:"])

    def test_repeated_invalid_dynamic_emojis_disable_dynamic_mode_and_fallback_to_static(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\n", encoding="utf-8")
            emojis_file = Path(tmp_dir) / "emojis.txt"
            emojis_file.write_text(":bad1:\n:bad2:\n:bad3:\n:bad4:\n", encoding="utf-8")

            config = self.make_config(verbs_file)
            config.dynamic_emojis_enabled = True
            config.emojis_file = emojis_file
            config.status_emoji = ":static:"
            spinner = SlackStatusSpinner(config)
            fake_client = FakeWebClient(invalid_emojis={":bad1:", ":bad2:", ":bad3:", ":bad4:"})
            spinner.client = fake_client
            spinner._sleep_with_shutdown = lambda _seconds: False

            exit_code = spinner.run()

            self.assertEqual(exit_code, 0)
            self.assertFalse(spinner.dynamic_emojis_active)
            self.assertEqual(spinner.consecutive_emoji_errors, 0)
            self.assertEqual(fake_client.set_calls[0]["status_emoji"], ":static:")

    def test_invalid_static_emoji_after_dynamic_fallback_continues_without_emoji(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\n", encoding="utf-8")
            emojis_file = Path(tmp_dir) / "emojis.txt"
            emojis_file.write_text(":bad1:\n:bad2:\n:bad3:\n:bad4:\n", encoding="utf-8")

            config = self.make_config(verbs_file)
            config.dynamic_emojis_enabled = True
            config.emojis_file = emojis_file
            config.status_emoji = ":badstatic:"
            spinner = SlackStatusSpinner(config)
            fake_client = FakeWebClient(
                invalid_emojis={":bad1:", ":bad2:", ":bad3:", ":bad4:", ":badstatic:"}
            )
            spinner.client = fake_client
            spinner._sleep_with_shutdown = lambda _seconds: False

            exit_code = spinner.run()

            self.assertEqual(exit_code, 0)
            self.assertFalse(spinner.dynamic_emojis_active)
            self.assertFalse(spinner.static_emoji_active)
            self.assertEqual(fake_client.set_calls[0]["status_emoji"], "")

    def test_invalid_static_emoji_falls_back_to_no_emoji(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            verbs_file = Path(tmp_dir) / "verbs.txt"
            verbs_file.write_text("Thinking\n", encoding="utf-8")

            config = self.make_config(verbs_file)
            config.status_emoji = ":badstatic:"
            spinner = SlackStatusSpinner(config)
            fake_client = FakeWebClient(invalid_emojis={":badstatic:"})
            spinner.client = fake_client
            spinner._sleep_with_shutdown = lambda _seconds: False

            exit_code = spinner.run()

            self.assertEqual(exit_code, 0)
            self.assertFalse(spinner.static_emoji_active)
            self.assertEqual(fake_client.attempted_profiles[0]["status_emoji"], ":badstatic:")
            self.assertEqual(fake_client.set_calls[0]["status_emoji"], "")


class ProjectConfigurationTests(unittest.TestCase):
    def test_env_example_contains_expected_keys(self):
        self.assertEqual(parse_env_keys(ENV_EXAMPLE_PATH), EXPECTED_ENV_KEYS)

    def test_env_matches_env_example_shape_when_present(self):
        if not ENV_PATH.exists():
            self.skipTest(".env no existe en este entorno local.")

        self.assertEqual(parse_env_keys(ENV_PATH), parse_env_keys(ENV_EXAMPLE_PATH))

    def test_load_config_uses_project_defaults(self):
        env = {
            "SLACK_USER_TOKEN": "xoxp-test-token",
            "STATUS_EMOJI": DEFAULT_STATUS_EMOJI,
            "ENABLE_DYNAMIC_EMOJIS": "false",
            "EMOJIS_FILE": "emojis.txt",
            "STATUS_PREFIX": "",
            "STATUS_SUFFIX": DEFAULT_STATUS_SUFFIX,
            "VERB_ORDER": "rotate",
            "VERBS_FILE": "spinner_verbs.txt",
        }
        with patch("slack_status_spinner.load_dotenv", return_value=True), patch.dict(os.environ, env, clear=True):
            config = load_config()

        self.assertEqual(config.interval_seconds, DEFAULT_INTERVAL_SECONDS)
        expected_expiration = max(
            DEFAULT_INTERVAL_SECONDS + DEFAULT_EXPIRATION_PADDING_SECONDS,
            DEFAULT_INTERVAL_SECONDS * 2,
        )
        self.assertEqual(config.expiration_seconds, expected_expiration)
        self.assertEqual(config.status_emoji, DEFAULT_STATUS_EMOJI)
        self.assertFalse(config.dynamic_emojis_enabled)
        self.assertEqual(config.status_suffix, DEFAULT_STATUS_SUFFIX)
        self.assertEqual(config.verb_order, "rotate")

    def test_load_config_requires_emojis_file_when_dynamic_emojis_enabled(self):
        env = {
            "SLACK_USER_TOKEN": "xoxp-test-token",
            "ENABLE_DYNAMIC_EMOJIS": "true",
            "EMOJIS_FILE": "",
            "STATUS_EMOJI": DEFAULT_STATUS_EMOJI,
            "STATUS_SUFFIX": DEFAULT_STATUS_SUFFIX,
            "VERB_ORDER": "rotate",
            "VERBS_FILE": "spinner_verbs.txt",
        }
        with patch("slack_status_spinner.load_dotenv", return_value=True), patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                load_config()

    def test_emojis_file_contains_entries(self):
        emojis_path = REPO_ROOT / "emojis.txt"
        emojis = [
            line.strip()
            for line in emojis_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertGreater(len(emojis), 0)

    def test_wrapper_script_has_valid_shell_syntax(self):
        subprocess.run(["bash", "-n", str(WRAPPER_PATH)], check=True, cwd=REPO_ROOT)

    def test_spinner_verbs_file_contains_entries(self):
        verbs = [
            line.strip()
            for line in VERBS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertGreater(len(verbs), 0)


if __name__ == "__main__":
    unittest.main()
