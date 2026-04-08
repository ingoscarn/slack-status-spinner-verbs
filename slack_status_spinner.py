#!/usr/bin/env python3
"""Rotate the current Slack user's custom status from a local verbs file."""

from __future__ import annotations

import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


DEFAULT_INTERVAL_SECONDS = 10
DEFAULT_EXPIRATION_PADDING_SECONDS = 30
DEFAULT_STATUS_EMOJI = ":thought_balloon:"
DEFAULT_STATUS_SUFFIX = "..."


@dataclass
class Config:
    slack_user_token: str
    verbs_file: Path
    status_emoji: str
    status_prefix: str
    status_suffix: str
    interval_seconds: int
    expiration_seconds: int
    verb_order: str


@dataclass
class SavedStatus:
    status_text: str
    status_emoji: str
    status_expiration: int


class SlackStatusSpinner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = WebClient(token=config.slack_user_token)
        self.shutdown_requested = False
        self.original_status: SavedStatus | None = None

    def run(self) -> int:
        verbs = self._load_verbs()
        self.original_status = self._fetch_current_status()
        self._install_signal_handlers()
        self._log(
            "Starting status spinner",
            interval=self.config.interval_seconds,
            expiration=self.config.expiration_seconds,
            verb_count=len(verbs),
            order=self.config.verb_order,
        )

        try:
            for verb in self._verb_sequence(verbs):
                if self.shutdown_requested:
                    break
                status_text = self._compose_status_text(verb)
                updated = self._attempt_status_update(status_text)
                if not updated:
                    break
                if not self._sleep_with_shutdown(self.config.interval_seconds):
                    break
        finally:
            self._restore_previous_status()

        return 0

    def _load_verbs(self) -> list[str]:
        if not self.config.verbs_file.exists():
            raise RuntimeError(f"Verb file not found: {self.config.verbs_file}")

        verbs = [
            line.strip()
            for line in self.config.verbs_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        if not verbs:
            raise RuntimeError(
                f"Verb file is empty or invalid: {self.config.verbs_file}. "
                "Add at least one non-empty line."
            )

        return verbs

    def _fetch_current_status(self) -> SavedStatus:
        try:
            response = self.client.users_profile_get()
        except SlackApiError as exc:
            raise RuntimeError(self._describe_slack_error("Failed to read current Slack status", exc)) from exc

        profile = response.get("profile", {})
        status = SavedStatus(
            status_text=profile.get("status_text", "") or "",
            status_emoji=profile.get("status_emoji", "") or "",
            status_expiration=int(profile.get("status_expiration", 0) or 0),
        )
        self._log(
            "Captured original Slack status",
            status_text=status.status_text or "<empty>",
            status_emoji=status.status_emoji or "<empty>",
            status_expiration=status.status_expiration,
        )
        return status

    def _install_signal_handlers(self) -> None:
        def _handle_signal(signum: int, _frame: object) -> None:
            signal_name = signal.Signals(signum).name
            self._log("Shutdown requested", signal=signal_name)
            self.shutdown_requested = True

        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    def _verb_sequence(self, verbs: list[str]) -> Iterable[str]:
        ordered_verbs = list(verbs)
        if self.config.verb_order == "random":
            random.shuffle(ordered_verbs)

        while True:
            for verb in ordered_verbs:
                yield verb
            if self.config.verb_order == "random":
                random.shuffle(ordered_verbs)

    def _compose_status_text(self, verb: str) -> str:
        text = f"{self.config.status_prefix}{verb}{self.config.status_suffix}".strip()
        if len(text) > 100:
            raise RuntimeError(
                f"Composed status exceeds Slack's 100 character limit: {text!r}"
            )
        return text

    def _attempt_status_update(self, status_text: str) -> bool:
        backoff_seconds = 5
        while not self.shutdown_requested:
            try:
                expiration = int(time.time()) + self.config.expiration_seconds
                self.client.users_profile_set(
                    profile={
                        "status_text": status_text,
                        "status_emoji": self.config.status_emoji,
                        "status_expiration": expiration,
                    }
                )
                self._log(
                    "Updated Slack status",
                    status_text=status_text,
                    status_emoji=self.config.status_emoji,
                    expires_at=expiration,
                )
                return True
            except SlackApiError as exc:
                response = exc.response
                error_code = response.get("error", "unknown_error")
                headers = response.headers if response is not None else {}
                retry_after = headers.get("Retry-After") if headers else None

                if response.status_code == 429 or error_code == "ratelimited":
                    delay = int(retry_after or 30)
                    self._log("Slack rate limited the request", retry_after_seconds=delay)
                    if not self._sleep_with_shutdown(delay):
                        return False
                    continue

                if error_code in {"token_revoked", "token_expired", "invalid_auth", "not_authed", "missing_scope"}:
                    raise RuntimeError(self._describe_slack_error("Slack authentication failed", exc)) from exc

                if error_code in {"service_unavailable", "fatal_error"}:
                    self._log(
                        "Slack service is temporarily unavailable; retrying",
                        error=error_code,
                        retry_in_seconds=backoff_seconds,
                    )
                    if not self._sleep_with_shutdown(backoff_seconds):
                        return False
                    backoff_seconds = min(backoff_seconds * 2, 60)
                    continue

                raise RuntimeError(self._describe_slack_error("Slack rejected the status update", exc)) from exc
            except Exception as exc:  # Network and transport failures end up here.
                self._log(
                    "Transient network or client failure while updating Slack status; retrying",
                    error=str(exc),
                    retry_in_seconds=backoff_seconds,
                )
                if not self._sleep_with_shutdown(backoff_seconds):
                    return False
                backoff_seconds = min(backoff_seconds * 2, 60)

        return False

    def _restore_previous_status(self) -> None:
        if self.original_status is None:
            return

        now = int(time.time())
        original = self.original_status

        if original.status_expiration and original.status_expiration <= now:
            restore_payload = {
                "status_text": "",
                "status_emoji": "",
                "status_expiration": 0,
            }
            log_text = "Cleared expired original Slack status during shutdown"
        else:
            restore_payload = {
                "status_text": original.status_text,
                "status_emoji": original.status_emoji,
                "status_expiration": original.status_expiration,
            }
            log_text = "Restored original Slack status during shutdown"

        try:
            self.client.users_profile_set(profile=restore_payload)
            self._log(log_text, **restore_payload)
        except Exception as exc:
            self._log(
                "Failed to restore previous Slack status; the status will expire automatically",
                error=str(exc),
            )

    def _sleep_with_shutdown(self, seconds: int) -> bool:
        for _ in range(max(seconds, 0)):
            if self.shutdown_requested:
                return False
            time.sleep(1)
        return not self.shutdown_requested

    @staticmethod
    def _describe_slack_error(prefix: str, error: SlackApiError) -> str:
        response = error.response
        error_code = response.get("error", "unknown_error")
        return f"{prefix}: {error_code}"

    @staticmethod
    def _log(message: str, **fields: object) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        serialized_fields = " ".join(f"{key}={value!r}" for key, value in fields.items())
        if serialized_fields:
            print(f"[{timestamp}] {message} | {serialized_fields}")
        else:
            print(f"[{timestamp}] {message}")


def load_config() -> Config:
    load_dotenv()

    slack_user_token = os.getenv("SLACK_USER_TOKEN", "").strip()
    if not slack_user_token:
        raise RuntimeError("Missing SLACK_USER_TOKEN in environment.")

    verbs_file = Path(os.getenv("VERBS_FILE", "spinner_verbs.txt")).expanduser().resolve()
    status_emoji = os.getenv("STATUS_EMOJI", DEFAULT_STATUS_EMOJI).strip() or DEFAULT_STATUS_EMOJI
    status_prefix = os.getenv("STATUS_PREFIX", "").strip()
    status_suffix = os.getenv("STATUS_SUFFIX", DEFAULT_STATUS_SUFFIX).strip() or DEFAULT_STATUS_SUFFIX
    interval_seconds = parse_positive_int("UPDATE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)

    expiration_seconds = parse_positive_int(
        "STATUS_EXPIRATION_SECONDS",
        max(interval_seconds + DEFAULT_EXPIRATION_PADDING_SECONDS, interval_seconds * 2),
    )
    if expiration_seconds <= interval_seconds:
        raise RuntimeError("STATUS_EXPIRATION_SECONDS must be greater than UPDATE_INTERVAL_SECONDS.")

    verb_order = os.getenv("VERB_ORDER", "rotate").strip().lower() or "rotate"
    if verb_order not in {"rotate", "random"}:
        raise RuntimeError("VERB_ORDER must be either 'rotate' or 'random'.")

    return Config(
        slack_user_token=slack_user_token,
        verbs_file=verbs_file,
        status_emoji=status_emoji,
        status_prefix=f"{status_prefix} " if status_prefix else "",
        status_suffix=status_suffix,
        interval_seconds=interval_seconds,
        expiration_seconds=expiration_seconds,
        verb_order=verb_order,
    )


def parse_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer. Received: {raw_value!r}") from exc

    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero.")

    return value


def main() -> int:
    try:
        config = load_config()
        spinner = SlackStatusSpinner(config)
        return spinner.run()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
