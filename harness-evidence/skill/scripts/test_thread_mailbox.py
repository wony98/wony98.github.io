from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HELPER = Path(__file__).with_name("thread_mailbox.py")
SKILL_ROOT = HELPER.parent.parent
CLAUDE_POINTER = (
    SKILL_ROOT / "../../../.claude/skills/operate-collaboration-thread/SKILL.md"
).resolve()
POINTER_VERSION_RE = re.compile(
    r"(?:Expected helper version:|require exactly)\s*`(\d+\.\d+\.\d+)`"
)
MODE_START = b"<!-- THREAD:MODE:START -->"
MODE_END = b"<!-- THREAD:MODE:END -->"
PIN_START = b"<!-- THREAD:PINNED:START -->"
PIN_END = b"<!-- THREAD:PINNED:END -->"


def run_helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )


def helper_version() -> str:
    result = run_helper("--version")
    if result.returncode != 0:
        raise AssertionError(result.stderr or "helper --version failed")
    return result.stdout.strip()


def assert_pointer_version(pointer: Path, expected: str) -> bool:
    if not pointer.exists():
        return False
    content = pointer.read_text(encoding="utf-8")
    declared = POINTER_VERSION_RE.findall(content)
    if not declared:
        raise AssertionError("Claude pointer has no declared helper version")
    if set(declared) != {expected}:
        raise AssertionError(
            f"Claude pointer versions {sorted(set(declared))} do not match helper {expected}"
        )
    return True


def initialize(thread: Path, mode: str | None = None) -> subprocess.CompletedProcess[str]:
    args = ["--thread", str(thread), "init", "--title", "Regression"]
    if mode is not None:
        args.extend(("--mode", mode))
    return run_helper(*args)


def append_message(
    thread: Path,
    expected_last: int,
    body: str = "body",
    expected_mode: str | None = None,
    reply: str = "Y",
    in_reply_to: str | None = None,
    sender: str = "CODEX",
    receiver: str = "CLAUDE/OWNER",
    status: str = "OPEN",
    artifact: str | None = None,
    message_type: str = "REPORT",
) -> subprocess.CompletedProcess[str]:
    args = [
        "--thread",
        str(thread),
        "append",
        "--expect-last",
        str(expected_last),
        "--tag",
        "REGRESSION",
        "--type",
        message_type,
        "--sender",
        sender,
        "--receiver",
        receiver,
        "--title",
        "message",
        "--body",
        body,
        "--reply",
        reply,
        "--status",
        status,
    ]
    if expected_mode is not None:
        args.extend(("--expect-mode", expected_mode))
    if in_reply_to is not None:
        args.extend(("--in-reply-to", in_reply_to))
    if artifact is not None:
        args.extend(("--artifact", artifact))
    return run_helper(*args)


def marker_block(raw: bytes, start: bytes, end: bytes) -> bytes:
    start_index = raw.index(start)
    end_index = raw.index(end, start_index)
    end_line = raw.find(b"\n", end_index)
    end_after = len(raw) if end_line == -1 else end_line + 1
    return raw[start_index:end_after]


def message_region(raw: bytes) -> bytes:
    index = raw.find(b"### [")
    return b"" if index == -1 else raw[index:]


class ThreadMailboxRegressionTests(unittest.TestCase):
    def test_helper_version_is_0_7_0(self) -> None:
        self.assertEqual(helper_version(), "0.7.0")

    def test_claude_pointer_matches_helper_version_when_present(self) -> None:
        if not CLAUDE_POINTER.exists():
            self.skipTest("Claude pointer is not installed")
        self.assertTrue(assert_pointer_version(CLAUDE_POINTER, helper_version()))
        self.assertNotIn("0.3.2", CLAUDE_POINTER.read_text(encoding="utf-8"))
        self.assertNotIn("0." + "4.0", CLAUDE_POINTER.read_text(encoding="utf-8"))

    def test_claude_pointer_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pointer = Path(temp_dir) / "SKILL.md"
            pointer.write_text(
                "Expected helper version: `0.0.0`.\n"
                "Run the helper and require exactly `0.0.0`.\n",
                encoding="utf-8",
            )
            with self.assertRaises(AssertionError):
                assert_pointer_version(pointer, helper_version())

    def test_claude_pointer_absence_is_skipped_by_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pointer = Path(temp_dir) / "missing" / "SKILL.md"
            self.assertFalse(assert_pointer_version(pointer, helper_version()))

    def test_inline_body_crlf_does_not_create_stray_cr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            created = initialize(thread)
            self.assertEqual(created.returncode, 0, created.stderr)

            raw = thread.read_bytes()
            self.assertNotIn(b"\r", raw)
            thread.write_bytes(raw.replace(b"\n", b"\r\n"))

            appended = append_message(
                thread,
                0,
                body="first line\r\n\r\n| second | line |",
            )
            self.assertEqual(appended.returncode, 0, appended.stderr)

            result = thread.read_bytes()
            self.assertNotIn(b"\r\r\n", result)
            self.assertIsNone(re.search(br"\r(?!\n)", result))

            validated = run_helper("--thread", str(thread), "validate")
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertIn("stray_cr=0", validated.stdout)

    def test_mode_show_and_set_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread).returncode, 0)
            shown = run_helper("--thread", str(thread), "mode", "--show")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("UNDECLARED", shown.stdout)

            updated = run_helper(
                "--thread",
                str(thread),
                "mode",
                "--set",
                "GIT",
                "--by",
                "OWNER",
                "--reason",
                "repository review",
            )
            self.assertEqual(updated.returncode, 0, updated.stderr)
            raw = thread.read_bytes()
            self.assertLess(raw.index(MODE_START), raw.index(PIN_START))
            shown = run_helper("--thread", str(thread), "mode", "--show")
            self.assertIn("mode GIT", shown.stdout)
            self.assertIn("OWNER", shown.stdout)
            self.assertIn("repository review", shown.stdout)

    def test_expect_mode_four_conditions_for_append(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            legacy = base / "legacy.md"
            self.assertEqual(initialize(legacy).returncode, 0)
            self.assertEqual(append_message(legacy, 0).returncode, 0)
            no_block_with_expect = append_message(legacy, 1, expected_mode="GIT")
            self.assertEqual(no_block_with_expect.returncode, 5)

            declared = base / "declared.md"
            self.assertEqual(initialize(declared, "GIT").returncode, 0)
            missing = append_message(declared, 0)
            mismatch = append_message(declared, 0, expected_mode="LOCAL")
            correct = append_message(declared, 0, expected_mode="GIT")
            self.assertEqual(missing.returncode, 5)
            self.assertEqual(mismatch.returncode, 5)
            self.assertEqual(correct.returncode, 0, correct.stderr)

    def test_resolve_and_pin_require_expected_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(append_message(thread, 0, expected_mode="GIT").returncode, 0)

            missing_resolve = run_helper(
                "--thread", str(thread), "resolve", "--number", "1", "--status", "DONE"
            )
            good_resolve = run_helper(
                "--thread",
                str(thread),
                "resolve",
                "--number",
                "1",
                "--status",
                "DONE",
                "--expect-mode",
                "GIT",
            )
            missing_pin = run_helper("--thread", str(thread), "pin", "--show-hash")
            good_pin = run_helper(
                "--thread", str(thread), "pin", "--show-hash", "--expect-mode", "GIT"
            )
            self.assertEqual(missing_resolve.returncode, 5)
            self.assertEqual(good_resolve.returncode, 0, good_resolve.stderr)
            self.assertEqual(missing_pin.returncode, 5)
            self.assertEqual(good_pin.returncode, 0, good_pin.stderr)

    def test_pin_and_mode_preserve_each_other_and_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            thread = root / "THREAD.md"
            body_file = root / "pin.md"
            body_file.write_text("## Updated\n\n- fact\n", encoding="utf-8")
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(append_message(thread, 0, expected_mode="GIT").returncode, 0)

            before = thread.read_bytes()
            mode_before = marker_block(before, MODE_START, MODE_END)
            messages_before = message_region(before)
            pin_show = run_helper(
                "--thread", str(thread), "pin", "--show-hash", "--expect-mode", "GIT"
            )
            pin_hash = re.search(r"([0-9a-f]{64})", pin_show.stdout).group(1)
            pin_update = run_helper(
                "--thread",
                str(thread),
                "pin",
                "--expect-hash",
                pin_hash,
                "--body-file",
                str(body_file),
                "--expect-mode",
                "GIT",
            )
            self.assertEqual(pin_update.returncode, 0, pin_update.stderr)
            after_pin = thread.read_bytes()
            self.assertEqual(marker_block(after_pin, MODE_START, MODE_END), mode_before)
            self.assertEqual(message_region(after_pin), messages_before)

            pin_before_mode = marker_block(after_pin, PIN_START, PIN_END)
            messages_before_mode = message_region(after_pin)
            mode_update = run_helper(
                "--thread",
                str(thread),
                "mode",
                "--set",
                "LOCAL",
                "--by",
                "OWNER",
                "--reason",
                "local documents",
                "--expect-current-mode",
                "GIT",
            )
            self.assertEqual(mode_update.returncode, 0, mode_update.stderr)
            after_mode = thread.read_bytes()
            self.assertEqual(marker_block(after_mode, PIN_START, PIN_END), pin_before_mode)
            self.assertEqual(message_region(after_mode), messages_before_mode)

    def test_mode_command_rejects_markers_only_in_message_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread).returncode, 0)
            body = "<!-- THREAD:MODE:START -->\nexample\n<!-- THREAD:MODE:END -->"
            self.assertEqual(append_message(thread, 0, body=body).returncode, 0)
            validated = run_helper("--thread", str(thread), "validate")
            shown = run_helper("--thread", str(thread), "mode", "--show")
            setting = run_helper(
                "--thread",
                str(thread),
                "mode",
                "--set",
                "GIT",
                "--by",
                "OWNER",
                "--reason",
                "review",
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertNotEqual(shown.returncode, 0)
            self.assertNotEqual(setting.returncode, 0)

    def test_malformed_mode_inputs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)

            def malformed(name: str, transform) -> subprocess.CompletedProcess[str]:
                thread = base / f"{name}.md"
                self.assertEqual(initialize(thread, "GIT").returncode, 0)
                thread.write_bytes(transform(thread.read_bytes()))
                return run_helper("--thread", str(thread), "validate")

            cases = {
                "duplicate_marker": lambda raw: raw.replace(MODE_START, MODE_START + b"\n" + MODE_START, 1),
                "reverse_marker": lambda raw: raw.replace(MODE_START, b"MODE-TEMP", 1)
                .replace(MODE_END, MODE_START, 1)
                .replace(b"MODE-TEMP", MODE_END, 1),
                "nested_marker": lambda raw: raw.replace(
                    MODE_END + b"\n\n" + PIN_START,
                    PIN_START + b"\n" + MODE_END,
                    1,
                ),
                "invalid_enum": lambda raw: raw.replace(b"- \xeb\xaa\xa8\xeb\x93\x9c: GIT", b"- \xeb\xaa\xa8\xeb\x93\x9c: git", 1),
                "duplicate_field": lambda raw: raw.replace(
                    b"- \xeb\xaa\xa8\xeb\x93\x9c: GIT\n",
                    b"- \xeb\xaa\xa8\xeb\x93\x9c: GIT\n- \xeb\xaa\xa8\xeb\x93\x9c: LOCAL\n",
                    1,
                ),
                "mode_after_pin": lambda raw: marker_block(raw, PIN_START, PIN_END)
                + b"\n"
                + marker_block(raw, MODE_START, MODE_END)
                + message_region(raw),
            }
            for name, transform in cases.items():
                with self.subTest(name=name):
                    result = malformed(name, transform)
                    self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_init_mode_is_optional_and_case_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            legacy = base / "legacy.md"
            local = base / "local.md"
            invalid = base / "invalid.md"
            self.assertEqual(initialize(legacy).returncode, 0)
            self.assertNotIn(MODE_START, legacy.read_bytes())
            validated = run_helper("--thread", str(legacy), "validate")
            self.assertIn("mode=UNDECLARED", validated.stdout)

            self.assertEqual(initialize(local, "LOCAL").returncode, 0)
            shown = run_helper("--thread", str(local), "mode", "--show")
            self.assertIn("mode LOCAL", shown.stdout)
            bad = initialize(invalid, "Local")
            self.assertNotEqual(bad.returncode, 0)

    def test_mode_set_rejects_stale_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            shown = run_helper("--thread", str(thread), "mode", "--show")
            current_hash = re.search(r"hash=([0-9a-f]{64})", shown.stdout).group(1)
            stale = run_helper(
                "--thread",
                str(thread),
                "mode",
                "--set",
                "LOCAL",
                "--by",
                "OWNER",
                "--reason",
                "local",
                "--expect-hash",
                "0" * 64,
            )
            correct = run_helper(
                "--thread",
                str(thread),
                "mode",
                "--set",
                "LOCAL",
                "--by",
                "OWNER",
                "--reason",
                "local",
                "--expect-hash",
                current_hash,
            )
            self.assertEqual(stale.returncode, 5)
            self.assertEqual(correct.returncode, 0, correct.stderr)

    def test_append_stdout_remains_backward_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            appended = append_message(thread, 0, expected_mode="GIT")
            self.assertEqual(appended.returncode, 0, appended.stderr)
            match = re.match(r"^\[thread\] appended #(\d{4}) -> ", appended.stdout)
            self.assertIsNotNone(match, appended.stdout)
            self.assertEqual(match.group(1), "0001")
            self.assertIn("mode=GIT", appended.stderr)
            self.assertIn("awaiting_reply=1", appended.stdout)

    def test_in_reply_to_records_structured_metadata_and_clears_awaiting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            first = append_message(thread, 0, expected_mode="GIT")
            second = append_message(
                thread,
                1,
                expected_mode="GIT",
            )
            third = append_message(
                thread,
                2,
                expected_mode="GIT",
                reply="N",
                in_reply_to="1,2",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertIn("- 회신대상: #0001,#0002", thread.read_text(encoding="utf-8"))
            self.assertIn("awaiting_reply=0", third.stdout)
            resolved = run_helper(
                "--thread",
                str(thread),
                "resolve",
                "--number",
                "1",
                "--status",
                "DONE",
                "--expect-mode",
                "GIT",
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertIn("- 회신대상: #0001,#0002", thread.read_text(encoding="utf-8"))

    def test_append_requires_explicit_reply_without_mutating_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            before = thread.read_bytes()
            missing = run_helper(
                "--thread",
                str(thread),
                "append",
                "--sender",
                "CODEX-BUILD",
                "--receiver",
                "OWNER",
                "--type",
                "REPORT",
                "--tag",
                "REGRESSION",
                "--title",
                "missing reply",
                "--status",
                "DONE",
                "--expect-last",
                "0",
                "--expect-mode",
                "GIT",
                "--body",
                "body",
            )
            self.assertEqual(missing.returncode, 2)
            self.assertIn(
                "질문·검증 요청은 Y, 완료 보고·공유는 N",
                missing.stderr,
            )
            self.assertEqual(thread.read_bytes(), before)

    def test_in_reply_to_rejects_future_missing_and_duplicate_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(append_message(thread, 0, expected_mode="GIT").returncode, 0)
            for value in ("2", "99", "1,1"):
                with self.subTest(value=value):
                    result = append_message(
                        thread,
                        1,
                        expected_mode="GIT",
                        reply="N",
                        in_reply_to=value,
                    )
                    self.assertNotEqual(result.returncode, 0)
            validated = run_helper("--thread", str(thread), "validate")
            self.assertIn("last=#0001", validated.stdout)

    def test_awaiting_uses_explicit_links_not_status_or_freeform_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(append_message(thread, 0, expected_mode="GIT").returncode, 0)
            mention = append_message(
                thread,
                1,
                body="관련 자료는 #0001 입니다.",
                expected_mode="GIT",
                reply="N",
            )
            self.assertIn("awaiting_reply=1", mention.stdout)
            review = run_helper(
                "--thread",
                str(thread),
                "resolve",
                "--number",
                "1",
                "--status",
                "REVIEW",
                "--expect-mode",
                "GIT",
            )
            self.assertEqual(review.returncode, 0, review.stderr)
            self.assertIn("awaiting_reply=1", review.stdout)
            linked = append_message(
                thread,
                2,
                expected_mode="GIT",
                reply="N",
                in_reply_to="1",
            )
            self.assertIn("awaiting_reply=0", linked.stdout)

    def test_legacy_exact_processed_message_line_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(append_message(thread, 0, expected_mode="GIT").returncode, 0)
            legacy = append_message(
                thread,
                1,
                body="처리한 메시지: `#0001`",
                expected_mode="GIT",
                reply="N",
            )
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertIn("awaiting_reply=0", legacy.stdout)
            self.assertNotIn("- 회신대상:", thread.read_text(encoding="utf-8"))

    def test_open_awaiting_format_and_mutual_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(append_message(thread, 0, expected_mode="GIT").returncode, 0)
            opened = run_helper("--thread", str(thread), "open", "--awaiting")
            conflict = run_helper("--thread", str(thread), "open", "--all", "--awaiting")
            self.assertEqual(opened.returncode, 0, opened.stderr)
            self.assertRegex(opened.stdout, r"#0001 · CODEX → CLAUDE/OWNER · message")
            self.assertIn("awaiting_reply=1", opened.stdout)
            self.assertNotEqual(conflict.returncode, 0)

    def test_intake_combines_mode_pinned_pending_and_bounded_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "LOCAL").returncode, 0)
            first = append_message(
                thread,
                0,
                body="A" * 200,
                expected_mode="LOCAL",
                sender="CLAUDE",
                receiver="CODEX",
            )
            second = append_message(
                thread,
                1,
                body="not for codex",
                expected_mode="LOCAL",
                sender="CODEX",
                receiver="CLAUDE",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)

            result = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX",
                "--limit",
                "3",
                "--recent",
                "0",
                "--body-chars",
                "40",
                "--pinned-chars",
                "80",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mode=LOCAL", result.stdout)
            self.assertIn("pinned_changed=UNKNOWN", result.stdout)
            self.assertIn("pending_for=1", result.stdout)
            self.assertIn("latest_OPEN=#0001", result.stdout)
            self.assertIn("latest_REVIEW=NONE", result.stdout)
            self.assertIn("latest_BLOCKED=NONE", result.stdout)
            self.assertIn("[PINNED", result.stdout)
            self.assertIn("[MESSAGE #0001", result.stdout)
            self.assertNotIn("[MESSAGE #0002", result.stdout)
            self.assertIn("truncated 160 chars", result.stdout)

    def test_intake_reports_pinned_change_and_latest_each_pending_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "LOCAL").returncode, 0)
            pin = run_helper(
                "--thread", str(thread), "pin", "--show-hash", "--expect-mode", "LOCAL"
            )
            pin_hash = re.search(r"([0-9a-f]{64})", pin.stdout).group(1)
            self.assertEqual(
                append_message(thread, 0, expected_mode="LOCAL", receiver="CODEX").returncode,
                0,
            )
            self.assertEqual(
                append_message(
                    thread,
                    1,
                    expected_mode="LOCAL",
                    receiver="CODEX",
                    status="REVIEW",
                ).returncode,
                0,
            )
            self.assertEqual(
                append_message(
                    thread,
                    2,
                    expected_mode="LOCAL",
                    receiver="CODEX",
                    status="BLOCKED",
                ).returncode,
                0,
            )
            self.assertEqual(
                append_message(thread, 3, expected_mode="LOCAL", receiver="CODEX").returncode,
                0,
            )
            unchanged = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX",
                "--limit",
                "1",
                "--recent",
                "0",
                "--known-pinned-hash",
                pin_hash,
            )
            changed = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX",
                "--limit",
                "1",
                "--recent",
                "0",
                "--known-pinned-hash",
                "0" * 64,
            )
            self.assertIn("pinned_changed=NO", unchanged.stdout)
            self.assertIn("pinned_changed=YES", changed.stdout)
            self.assertIn("[PINNED] unchanged; body omitted", unchanged.stdout)
            self.assertNotIn("## Pinned context", unchanged.stdout)
            self.assertIn("## Pinned context", changed.stdout)
            self.assertIn("latest_OPEN=#0004", unchanged.stdout)
            self.assertIn("latest_REVIEW=#0002", unchanged.stdout)
            self.assertIn("latest_BLOCKED=#0003", unchanged.stdout)
            for number in (2, 3, 4):
                self.assertIn(f"[MESSAGE #{number:04d}", unchanged.stdout)

    def test_intake_known_last_pages_all_exact_agent_messages_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            receivers = (
                "CODEX-BUILD",
                "CODEX-REVIEW",
                "ALL",
                "CODEX-BUILD",
                "CODEX-BUILD",
            )
            for index, receiver in enumerate(receivers):
                self.assertEqual(
                    append_message(
                        thread,
                        index,
                        expected_mode="GIT",
                        reply="N",
                        receiver=receiver,
                    ).returncode,
                    0,
                )

            first = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX-BUILD",
                "--known-last",
                "0",
                "--limit",
                "3",
                "--recent",
                "9",
            )
            second = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX-BUILD",
                "--known-last",
                "4",
                "--limit",
                "3",
            )
            plain_codex = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX",
                "--known-last",
                "0",
                "--limit",
                "10",
            )
            reviewer = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX-REVIEW",
                "--known-last",
                "0",
                "--limit",
                "10",
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("matched=4 shown=3 remaining=1", first.stdout)
            self.assertIn("next_after=#0004 next_range=#0005-#0005", first.stdout)
            self.assertLess(
                first.stdout.index("[MESSAGE #0001"),
                first.stdout.index("[MESSAGE #0003"),
            )
            self.assertLess(
                first.stdout.index("[MESSAGE #0003"),
                first.stdout.index("[MESSAGE #0004"),
            )
            self.assertNotIn("[MESSAGE #0002", first.stdout)
            self.assertNotIn("[MESSAGE #0005", first.stdout)
            self.assertIn("matched=1 shown=1 remaining=0", second.stdout)
            self.assertIn("[MESSAGE #0005", second.stdout)
            self.assertNotIn("[MESSAGE #0004", second.stdout)
            self.assertIn("matched=1 shown=1", plain_codex.stdout)
            self.assertIn("[MESSAGE #0003", plain_codex.stdout)
            self.assertNotIn("[MESSAGE #0001", plain_codex.stdout)
            self.assertIn("matched=2 shown=2", reviewer.stdout)
            self.assertIn("[MESSAGE #0002", reviewer.stdout)
            self.assertIn("[MESSAGE #0003", reviewer.stdout)

    def test_intake_known_last_boundaries_and_all_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            cases = (
                ("OPEN", "REPORT"),
                ("REVIEW", "VERIFY-REQ"),
                ("BLOCKED", "ASK"),
                ("DONE", "FYI"),
            )
            for index, (status, message_type) in enumerate(cases):
                self.assertEqual(
                    append_message(
                        thread,
                        index,
                        expected_mode="GIT",
                        reply="N",
                        receiver="CODEX-BUILD",
                        status=status,
                        message_type=message_type,
                    ).returncode,
                    0,
                )
            all_new = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX-BUILD",
                "--known-last",
                "0",
                "--limit",
                "10",
            )
            at_last = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX-BUILD",
                "--known-last",
                "4",
            )
            negative = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX-BUILD",
                "--known-last",
                "-1",
            )
            future = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX-BUILD",
                "--known-last",
                "5",
            )
            zero_limit = run_helper(
                "--thread",
                str(thread),
                "intake",
                "--agent",
                "CODEX-BUILD",
                "--known-last",
                "0",
                "--limit",
                "0",
            )
            self.assertIn("matched=4 shown=4 remaining=0", all_new.stdout)
            for number in range(1, 5):
                self.assertIn(f"[MESSAGE #{number:04d}", all_new.stdout)
            self.assertIn("matched=0 shown=0 remaining=0", at_last.stdout)
            for refused in (negative, future, zero_limit):
                self.assertEqual(refused.returncode, 2)

    def test_open_awaiting_filters_exact_agent_and_after_number(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            receivers = ("CODEX-BUILD", "CODEX-REVIEW", "ALL", "CODEX-BUILD")
            for index, receiver in enumerate(receivers):
                self.assertEqual(
                    append_message(
                        thread,
                        index,
                        expected_mode="GIT",
                        receiver=receiver,
                    ).returncode,
                    0,
                )
            filtered = run_helper(
                "--thread",
                str(thread),
                "open",
                "--awaiting",
                "--agent",
                "CODEX-BUILD",
                "--after-number",
                "2",
            )
            review = run_helper(
                "--thread",
                str(thread),
                "open",
                "--awaiting",
                "--agent",
                "CODEX-REVIEW",
                "--after-number",
                "0",
            )
            legacy = run_helper("--thread", str(thread), "open", "--awaiting")
            invalid = run_helper(
                "--thread", str(thread), "open", "--agent", "CODEX-BUILD"
            )
            negative = run_helper(
                "--thread",
                str(thread),
                "open",
                "--awaiting",
                "--agent",
                "CODEX-BUILD",
                "--after-number",
                "-1",
            )
            self.assertEqual(filtered.returncode, 0, filtered.stderr)
            self.assertIn("matched=2", filtered.stdout)
            self.assertIn("#0003", filtered.stdout)
            self.assertIn("#0004", filtered.stdout)
            self.assertNotIn("#0001", filtered.stdout)
            self.assertNotIn("#0002 ·", filtered.stdout)
            self.assertIn("#0002", review.stdout)
            self.assertIn("#0003", review.stdout)
            self.assertNotIn("#0001", review.stdout)
            for number in range(1, 5):
                self.assertIn(f"#{number:04d}", legacy.stdout)
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(negative.returncode, 2)

    def test_show_prints_one_complete_message_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread).returncode, 0)
            self.assertEqual(append_message(thread, 0, body="first unique").returncode, 0)
            self.assertEqual(append_message(thread, 1, body="second unique").returncode, 0)
            shown = run_helper("--thread", str(thread), "show", "--number", "1")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("first unique", shown.stdout)
            self.assertNotIn("second unique", shown.stdout)

    def test_show_with_links_follows_structured_prior_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread).returncode, 0)
            self.assertEqual(append_message(thread, 0, body="root evidence").returncode, 0)
            self.assertEqual(
                append_message(thread, 1, body="middle reply", in_reply_to="1").returncode,
                0,
            )
            self.assertEqual(
                append_message(thread, 2, body="latest reply", in_reply_to="2").returncode,
                0,
            )
            shown = run_helper(
                "--thread",
                str(thread),
                "show",
                "--number",
                "3",
                "--with-links",
                "--depth",
                "2",
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("LINKED-PRIOR #0001", shown.stdout)
            self.assertIn("LINKED-PRIOR #0002", shown.stdout)
            self.assertIn("REQUESTED #0003", shown.stdout)
            self.assertLess(shown.stdout.index("root evidence"), shown.stdout.index("latest reply"))

    def test_append_long_body_is_always_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread).returncode, 0)
            body = "x" * 6001
            refused = append_message(thread, 0, body=body)
            self.assertEqual(refused.returncode, 6)
            self.assertIn("default maximum is 6000", refused.stderr)
            self.assertIn("--artifact", refused.stderr)
            self.assertIn("--in-reply-to", refused.stderr)

    def test_artifact_requires_and_records_reply_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread).returncode, 0)
            self.assertEqual(append_message(thread, 0).returncode, 0)
            missing_link = append_message(thread, 1, artifact="D:\\report.md", reply="N")
            linked = append_message(
                thread,
                1,
                artifact="D:\\report.md",
                in_reply_to="1",
                reply="N",
            )
            self.assertEqual(missing_link.returncode, 2)
            self.assertIn("requires --in-reply-to", missing_link.stderr)
            self.assertEqual(linked.returncode, 0, linked.stderr)
            text = thread.read_text(encoding="utf-8")
            self.assertIn("- 회신대상: #0001", text)
            self.assertIn("- 산출물: D:\\report.md", text)

    def test_awaiting_does_not_block_mutation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(append_message(thread, 0, expected_mode="GIT").returncode, 0)
            second = append_message(thread, 1, expected_mode="GIT", reply="N")
            resolved = run_helper(
                "--thread",
                str(thread),
                "resolve",
                "--number",
                "2",
                "--status",
                "DONE",
                "--expect-mode",
                "GIT",
            )
            pinned = run_helper(
                "--thread", str(thread), "pin", "--show-hash", "--expect-mode", "GIT"
            )
            shown = run_helper("--thread", str(thread), "mode", "--show")
            for result in (second, resolved, pinned, shown):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("awaiting_reply=1", result.stdout)

    def test_done_is_terminal_but_review_does_not_imply_a_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(append_message(thread, 0, expected_mode="GIT").returncode, 0)
            review = run_helper(
                "--thread",
                str(thread),
                "resolve",
                "--number",
                "1",
                "--status",
                "REVIEW",
                "--expect-mode",
                "GIT",
            )
            done = run_helper(
                "--thread",
                str(thread),
                "resolve",
                "--number",
                "1",
                "--status",
                "DONE",
                "--expect-mode",
                "GIT",
            )
            self.assertIn("awaiting_reply=1", review.stdout)
            self.assertIn("awaiting_reply=0", done.stdout)

    def test_read_only_awaiting_checks_preserve_legacy_message_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            self.assertEqual(
                append_message(
                    thread,
                    0,
                    body="처리한 메시지: #9999 완료가 아니라 단순 문장입니다.",
                    expected_mode="GIT",
                ).returncode,
                0,
            )
            before = thread.read_bytes()
            validated = run_helper("--thread", str(thread), "validate")
            opened = run_helper("--thread", str(thread), "open", "--awaiting")
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(opened.returncode, 0, opened.stderr)
            self.assertEqual(thread.read_bytes(), before)
            self.assertIn("awaiting_reply=1", opened.stdout)

    def test_removed_terms_and_old_version_are_absent(self) -> None:
        checked = [
            HELPER,
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "agents" / "openai.yaml",
            SKILL_ROOT / "references" / "operating-rules.md",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in checked)
        self.assertNotIn("reply_" + "pending", combined)
        self.assertNotIn('TOOL_VERSION = "' + "0.4.0" + '"', combined)
        self.assertNotIn("process only the newest relevant message", combined)
        self.assertNotIn('TOOL_VERSION = "' + "0.6.0" + '"', combined)

    def test_agent_examples_list_every_receiver_token(self) -> None:
        """`--agent <...>` 예시가 실제 토큰 하나라도 빠뜨리면 실패한다.

        0.7 문서는 CODEX 를 빠뜨린 예시를 실었다. 그대로 실행하면 CODEX 앞으로 온
        메시지가 matched 0 으로 조용히 누락된다.
        """
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        examples = re.findall(r"--agent <([^>]+)>", text)
        self.assertTrue(examples, "SKILL.md 에 --agent 예시가 없다")
        required = {"CODEX", "CODEX-BUILD", "CODEX-REVIEW", "CLAUDE"}
        for example in examples:
            tokens = {token.strip() for token in example.split("|")}
            self.assertEqual(
                required - tokens,
                set(),
                f"--agent 예시에 빠진 토큰: {sorted(required - tokens)} (예시: {example})",
            )

    def test_mode_metadata_hygiene(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            for index, (setter, reason) in enumerate(
                (("A\nB", "reason"), ("OWNER", "THREAD:PINNED:START")), start=1
            ):
                thread = base / f"thread-{index}.md"
                self.assertEqual(initialize(thread).returncode, 0)
                result = run_helper(
                    "--thread",
                    str(thread),
                    "mode",
                    "--set",
                    "GIT",
                    "--by",
                    setter,
                    "--reason",
                    reason,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_concurrent_mode_set_has_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = Path(temp_dir) / "THREAD.md"
            self.assertEqual(initialize(thread, "GIT").returncode, 0)
            command = [
                sys.executable,
                str(HELPER),
                "--thread",
                str(thread),
                "mode",
                "--set",
                "LOCAL",
                "--by",
                "OWNER",
                "--reason",
                "concurrent",
                "--expect-current-mode",
                "GIT",
            ]
            first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            first.communicate(timeout=20)
            second.communicate(timeout=20)
            self.assertEqual(sorted((first.returncode, second.returncode)), [0, 5])
            shown = run_helper("--thread", str(thread), "mode", "--show")
            self.assertIn("mode LOCAL", shown.stdout)


if __name__ == "__main__":
    unittest.main()
