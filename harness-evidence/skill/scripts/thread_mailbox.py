# -*- coding: utf-8 -*-
"""Create and safely operate repository-backed agent collaboration threads."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


HEADER_RE = re.compile(r"^###\s+\[[^\]]+\].*?·\s+#(?P<num>\d{4})\b")
HEADER_DETAIL_RE = re.compile(
    r"^###\s+\[(?P<sender>[^\]]+?)\s+→\s+(?P<receiver>[^\]]+?)\]"
    r".*?·\s+#(?P<num>\d{4})\b.*?·\s+\[[^\]]+\]\s*(?P<title>.*)$"
)
LEGACY_REPLY_LINE_RE = re.compile(
    r"^처리한 메시지:\s+`?#\d{4}`?(?:\s*,\s*`?#\d{4}`?)*$"
)
REPLY_NUMBER_RE = re.compile(r"#(?P<num>\d{4})")
STATUSES = ("OPEN", "REVIEW", "DONE", "BLOCKED")
PENDING_STATUSES = ("OPEN", "REVIEW", "BLOCKED")
TYPES = ("ASK", "REPORT", "VERIFY-REQ", "VERIFY-RESULT", "DECISION", "FYI")
PIN_START = "<!-- THREAD:PINNED:START -->"
PIN_END = "<!-- THREAD:PINNED:END -->"
MODE_START = "<!-- THREAD:MODE:START -->"
MODE_END = "<!-- THREAD:MODE:END -->"
MODES = ("GIT", "LOCAL")
TOOL_VERSION = "0.7.0"
LOCK_TIMEOUT_SECONDS = 15.0
MAX_INLINE_BODY_CHARS = 6000


class ThreadArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "--reply" in message and "required" in message:
            message += "; --reply 안내: 질문·검증 요청은 Y, 완료 보고·공유는 N"
        super().error(message)


@dataclass(frozen=True)
class MarkerRegion:
    start: int
    body_start: int
    body_end: int
    end_after: int
    body: bytes


@dataclass(frozen=True)
class ThreadLayout:
    first_message: int
    mode: MarkerRegion | None
    pin: MarkerRegion | None


@dataclass(frozen=True)
class ModeInfo:
    value: str
    setter: str
    reason: str
    body_hash: str


@dataclass(frozen=True)
class MessageInfo:
    number: int
    line_no: int
    sender: str
    receiver: str
    title: str
    status: str
    reply_required: bool
    reply_targets: tuple[int, ...]


def parse_headers(text: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = HEADER_RE.match(line.strip())
        if match:
            found.append((int(match.group("num")), line_no))
    return found


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def decode_utf8(raw: bytes) -> str:
    """Decode strictly so a mutation never replaces undecodable bytes."""
    return raw.decode("utf-8")


def read_text(path: Path) -> str:
    return decode_utf8(read_bytes(path))


def first_message_offset(raw: bytes) -> int:
    """Return the first numbered-message byte offset, or EOF when none exists."""
    offset = 0
    for raw_line in raw.splitlines(keepends=True):
        line = decode_utf8(raw_line).strip()
        if HEADER_RE.match(line):
            return offset
        offset += len(raw_line)
    return len(raw)


def marker_region_in_prefix(
    prefix: bytes,
    start_text: str,
    end_text: str,
    label: str,
) -> MarkerRegion | None:
    start_marker = start_text.encode("ascii")
    end_marker = end_text.encode("ascii")
    start_count = prefix.count(start_marker)
    end_count = prefix.count(end_marker)
    if start_count == 0 and end_count == 0:
        return None
    if start_count != 1 or end_count != 1:
        raise ValueError(
            f"{label} requires exactly one start marker and one end marker "
            f"in the pre-message region (start={start_count}, end={end_count})"
        )
    start_index = prefix.index(start_marker)
    end_index = prefix.index(end_marker)
    if end_index <= start_index:
        raise ValueError(f"{label} markers are out of order")
    if start_index and prefix[start_index - 1 : start_index] != b"\n":
        raise ValueError(f"{label} start marker must begin on its own line")
    if end_index and prefix[end_index - 1 : end_index] != b"\n":
        raise ValueError(f"{label} end marker must begin on its own line")
    start_line_end = prefix.find(b"\n", start_index)
    if start_line_end == -1 or start_line_end >= end_index:
        raise ValueError(f"{label} start marker has no line ending")
    end_line_end = prefix.find(b"\n", end_index)
    end_after = len(prefix) if end_line_end == -1 else end_line_end + 1
    body_start = start_line_end + 1
    body = prefix[body_start:end_index]
    return MarkerRegion(start_index, body_start, end_index, end_after, body)


def thread_layout(raw: bytes) -> ThreadLayout:
    first_message = first_message_offset(raw)
    prefix = raw[:first_message]
    mode = marker_region_in_prefix(prefix, MODE_START, MODE_END, "mode")
    pin_region = marker_region_in_prefix(prefix, PIN_START, PIN_END, "pin")
    if mode is not None and pin_region is None:
        raise ValueError("mode block requires a separate pinned block after it")
    if mode is not None and pin_region is not None and mode.end_after > pin_region.start:
        raise ValueError("mode block must appear before the pinned block without nesting")
    return ThreadLayout(first_message=first_message, mode=mode, pin=pin_region)


def parse_mode_body(body: bytes) -> tuple[str, str, str]:
    text = decode_utf8(body).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in text.split("\n") if line]
    prefixes = ("- 모드: ", "- 설정: ", "- 사유: ")
    values: dict[str, str] = {}
    for prefix in prefixes:
        matching = [line for line in lines if line.startswith(prefix)]
        if len(matching) != 1:
            raise ValueError(f"mode field {prefix.strip()!r} occurs {len(matching)} times")
        values[prefix] = matching[0][len(prefix) :]
    if len(lines) != len(prefixes):
        raise ValueError("mode block contains unexpected or duplicate fields")
    mode_value = values["- 모드: "]
    if mode_value not in MODES:
        raise ValueError(f"invalid mode value {mode_value!r}; expected GIT or LOCAL")
    setting = values["- 설정: "]
    setter = setting.split(" · ", 1)[0].strip()
    reason = values["- 사유: "].strip()
    if not setter or not reason:
        raise ValueError("mode setter and reason must not be empty")
    return mode_value, setter, reason


def mode_info(raw: bytes, *, reject_message_only_markers: bool = False) -> ModeInfo | None:
    layout = thread_layout(raw)
    if layout.mode is None:
        if reject_message_only_markers:
            message_region = raw[layout.first_message :]
            if MODE_START.encode("ascii") in message_region or MODE_END.encode("ascii") in message_region:
                raise ValueError("mode markers exist only in the message region")
        return None
    value, setter, reason = parse_mode_body(layout.mode.body)
    return ModeInfo(
        value=value,
        setter=setter,
        reason=reason,
        body_hash=hashlib.sha256(layout.mode.body).hexdigest(),
    )


def validate_raw(raw: bytes, expected_last: int | None = None) -> tuple[bool, list[str], int]:
    text = decode_utf8(raw)
    ok, problems, last = validate_text(text, expected_last=expected_last)
    try:
        layout = thread_layout(raw)
        if layout.mode is not None:
            parse_mode_body(layout.mode.body)
    except ValueError as error:
        problems.append(str(error))
    return not problems and ok, problems, last


def newline_counts(text: str) -> tuple[int, int, int]:
    crlf = text.count("\r\n")
    without_crlf = text.replace("\r\n", "")
    lf_only = without_crlf.count("\n")
    stray_cr = without_crlf.count("\r")
    return crlf, lf_only, stray_cr


def newline_label(text: str) -> str:
    crlf, lf_only, stray_cr = newline_counts(text)
    if crlf and lf_only:
        return "MIXED"
    if crlf:
        return "CRLF"
    if lf_only:
        return "LF"
    return "NONE"


def newline_sequence(text: str) -> str:
    label = newline_label(text)
    if label == "MIXED":
        raise ValueError("thread contains mixed or stray line endings")
    return "\r\n" if label == "CRLF" else "\n"


def metadata_field_lines(
    lines: list[str], start: int, end: int, prefix: str
) -> list[int]:
    """Return matching rows only from the header metadata block, never the body."""
    matches: list[int] = []
    for index in range(start + 1, end):
        stripped = lines[index].rstrip("\r\n")
        if stripped == "- 본문:":
            break
        if stripped.startswith(prefix):
            matches.append(index)
    return matches


def metadata_state_lines(lines: list[str], start: int, end: int) -> list[int]:
    return metadata_field_lines(lines, start, end, "- 상태: ")


def parse_structured_reply_targets(value: str) -> tuple[int, ...]:
    stripped = value.strip()
    if not re.fullmatch(r"#\d{4}(?:\s*,\s*#\d{4})*", stripped):
        raise ValueError(
            "reply targets must use #NNNN comma-separated form, for example #0001,#0002"
        )
    numbers = tuple(int(match.group("num")) for match in REPLY_NUMBER_RE.finditer(stripped))
    if len(numbers) != len(set(numbers)):
        raise ValueError("reply targets must not contain duplicates")
    return numbers


def parse_cli_reply_targets(value: str | None) -> tuple[int, ...]:
    if value is None:
        return ()
    stripped = value.strip()
    if not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", stripped):
        raise ValueError("--in-reply-to must be a comma-separated list of message numbers")
    numbers = tuple(int(part.strip()) for part in stripped.split(","))
    if len(numbers) != len(set(numbers)):
        raise ValueError("--in-reply-to must not contain duplicate message numbers")
    return numbers


def legacy_reply_targets(lines: list[str], body_start: int, end: int) -> tuple[int, ...]:
    numbers: list[int] = []
    for index in range(body_start, end):
        stripped = lines[index].rstrip("\r\n")
        if not LEGACY_REPLY_LINE_RE.fullmatch(stripped):
            continue
        numbers.extend(int(match.group("num")) for match in REPLY_NUMBER_RE.finditer(stripped))
    return tuple(dict.fromkeys(numbers))


def message_infos(text: str) -> list[MessageInfo]:
    lines = text.splitlines(keepends=True)
    found = parse_headers(text)
    existing_numbers = {number for number, _ in found}
    messages: list[MessageInfo] = []
    for index, (number, line_no) in enumerate(found):
        start = line_no - 1
        end = found[index + 1][1] - 1 if index + 1 < len(found) else len(lines)
        header = lines[start].rstrip("\r\n")
        detail = HEADER_DETAIL_RE.match(header)
        if detail is None:
            sender, receiver, title = "?", "?", header
        else:
            sender = detail.group("sender").strip()
            receiver = detail.group("receiver").strip()
            title = detail.group("title").strip()

        reply_rows = metadata_field_lines(lines, start, end, "- 답필요: ")
        reply_required = len(reply_rows) == 1 and (
            lines[reply_rows[0]].rstrip("\r\n").removeprefix("- 답필요: ") == "Y"
        )

        state_rows = metadata_state_lines(lines, start, end)
        status = (
            lines[state_rows[0]].rstrip("\r\n").removeprefix("- 상태: ")
            if len(state_rows) == 1
            else ""
        )

        target_rows = metadata_field_lines(lines, start, end, "- 회신대상: ")
        if target_rows:
            value = lines[target_rows[0]].rstrip("\r\n").removeprefix("- 회신대상: ")
            targets = parse_structured_reply_targets(value)
        else:
            body_marker = next(
                (
                    row
                    for row in range(start + 1, end)
                    if lines[row].rstrip("\r\n") == "- 본문:"
                ),
                None,
            )
            body_start = body_marker + 1 if body_marker is not None else end
            targets = legacy_reply_targets(lines, body_start, end)

        valid_targets = tuple(
            target for target in targets if target in existing_numbers and target < number
        )
        messages.append(
            MessageInfo(
                number=number,
                line_no=line_no,
                sender=sender,
                receiver=receiver,
                title=title,
                status=status,
                reply_required=reply_required,
                reply_targets=valid_targets,
            )
        )
    return messages


def message_block(text: str, number: int) -> str:
    """Return one complete numbered message without reading unrelated history."""
    lines = text.splitlines(keepends=True)
    found = parse_headers(text)
    for index, (candidate, line_no) in enumerate(found):
        if candidate != number:
            continue
        start = line_no - 1
        end = found[index + 1][1] - 1 if index + 1 < len(found) else len(lines)
        return "".join(lines[start:end]).rstrip("\r\n")
    raise ValueError(f"message not found: #{number:04d}")


def message_body(text: str, number: int) -> str:
    block = message_block(text, number)
    marker = "- 본문:"
    lines = block.splitlines()
    try:
        index = lines.index(marker)
    except ValueError:
        return ""
    return "\n".join(lines[index + 1 :]).strip()


def receiver_matches(receiver: str, agent: str | None) -> bool:
    if not agent:
        return True
    tokens = {
        token
        for token in re.split(r"[^A-Za-z0-9_-]+", receiver.upper())
        if token
    }
    wanted = agent.upper()
    return wanted in tokens or "ALL" in tokens


def clipped(value: str, limit: int) -> str:
    if limit < 1 or len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit].rstrip()}\n[thread] ... truncated {omitted} chars"


def read_only_validation(raw: bytes, command: str) -> tuple[str, int] | None:
    text = decode_utf8(raw)
    ok, problems, last = validate_raw(raw)
    structural = [
        problem for problem in problems if not problem.startswith("mixed line endings:")
    ]
    if structural:
        for problem in structural:
            print(problem, file=sys.stderr)
        return None
    if not ok:
        print(
            f"[thread] warning: mixed newlines detected; read-only {command} is allowed, "
            "mutation commands will refuse",
            file=sys.stderr,
        )
    return text, last


def intake(path: Path, args: argparse.Namespace) -> int:
    """Emit one bounded mailbox snapshot for the common read/process path."""
    raw = read_bytes(path)
    validated = read_only_validation(raw, "intake")
    if validated is None:
        return 1
    text, last = validated
    messages = message_infos(text)
    awaiting_all = awaiting_reply_messages(text)
    relevant_pending = [
        message
        for message in messages
        if message.status in PENDING_STATUSES
        and receiver_matches(message.receiver, args.agent)
    ]
    relevant_awaiting = [
        message
        for message in awaiting_all
        if receiver_matches(message.receiver, args.agent)
    ]

    latest_by_status: dict[str, MessageInfo] = {}
    for message in relevant_pending:
        latest_by_status[message.status] = message

    incremental_summary: str | None = None
    if args.known_last is not None:
        if args.known_last < 0:
            print("[thread] intake refused: --known-last must be 0 or greater", file=sys.stderr)
            return 2
        if args.known_last > last:
            print(
                f"[thread] intake refused: --known-last #{args.known_last:04d} "
                f"is greater than actual last #{last:04d}",
                file=sys.stderr,
            )
            return 2
        if args.limit < 1:
            print(
                "[thread] intake refused: --limit must be at least 1 with --known-last",
                file=sys.stderr,
            )
            return 2
        incremental = [
            message
            for message in messages
            if message.number > args.known_last
            and receiver_matches(message.receiver, args.agent)
        ]
        shown = incremental[: args.limit]
        remaining = len(incremental) - len(shown)
        next_after = shown[-1].number if shown else args.known_last
        next_range = (
            f"#{incremental[len(shown)].number:04d}-#{incremental[-1].number:04d}"
            if remaining
            else "NONE"
        )
        incremental_summary = (
            f"[thread] incremental agent={args.agent} after=#{args.known_last:04d} "
            f"matched={len(incremental)} shown={len(shown)} remaining={remaining} "
            f"next_after=#{next_after:04d} next_range={next_range}"
        )
        selected_numbers = {message.number for message in shown}
    else:
        selected_numbers = {
            message.number for message in relevant_pending[-max(0, args.limit) :]
        }
        selected_numbers.update(
            message.number for message in latest_by_status.values()
        )
        if args.recent:
            selected_numbers.update(
                message.number for message in messages[-max(0, args.recent) :]
            )
    selected = [
        message for message in messages if message.number in selected_numbers
    ]

    info = mode_info(raw)
    mode_label = info.value if info is not None else "UNDECLARED"
    layout = thread_layout(raw)
    pin_digest = (
        hashlib.sha256(layout.pin.body).hexdigest()
        if layout.pin is not None
        else "NONE"
    )
    if args.known_pinned_hash is None:
        pinned_changed = "UNKNOWN"
    else:
        pinned_changed = (
            "NO"
            if args.known_pinned_hash.strip().lower() == pin_digest.lower()
            else "YES"
        )
    latest_labels = " ".join(
        f"latest_{status}=#{latest_by_status[status].number:04d}"
        if status in latest_by_status
        else f"latest_{status}=NONE"
        for status in PENDING_STATUSES
    )
    print(
        f"[thread] intake mode={mode_label} last=#{last:04d} messages={len(messages)} "
        f"pinned_hash={pin_digest} pinned_changed={pinned_changed} "
        f"pending_for={len(relevant_pending)} awaiting_for={len(relevant_awaiting)} "
        f"awaiting_total={len(awaiting_all)}"
    )
    print(f"[thread] {latest_labels}")
    if incremental_summary is not None:
        print(incremental_summary)

    if not args.no_pinned:
        if layout.pin is None:
            print("[PINNED] unavailable")
        elif pinned_changed == "NO":
            print("[PINNED] unchanged; body omitted")
        else:
            pin_text = decode_utf8(layout.pin.body).strip()
            print(f"[PINNED sha256={pin_digest}]")
            print(clipped(pin_text, args.pinned_chars) or "(empty)")

    if not selected:
        print("[MESSAGES] none selected")
        return 0
    for message in selected:
        flags = []
        if message in relevant_awaiting:
            flags.append("AWAITING")
        if message.status in PENDING_STATUSES:
            flags.append(message.status)
        flag_text = ",".join(dict.fromkeys(flags)) or message.status or "UNKNOWN"
        print(
            f"[MESSAGE #{message.number:04d} {flag_text}] "
            f"{message.sender} -> {message.receiver} | {message.title}"
        )
        print(clipped(message_body(text, message.number), args.body_chars) or "(empty body)")
    return 0


def linked_message_numbers(
    messages: list[MessageInfo], number: int, depth: int
) -> list[int]:
    by_number = {message.number: message for message in messages}
    if number not in by_number:
        raise ValueError(f"message not found: #{number:04d}")
    ordered: list[int] = []
    seen: set[int] = set()

    def visit(current: int, remaining: int) -> None:
        if current in seen:
            return
        seen.add(current)
        message = by_number[current]
        if remaining > 0:
            for target in message.reply_targets:
                visit(target, remaining - 1)
        ordered.append(current)

    visit(number, max(0, depth))
    return ordered


def show_message(path: Path, number: int, with_links: bool, depth: int) -> int:
    raw = read_bytes(path)
    validated = read_only_validation(raw, "show")
    if validated is None:
        return 1
    text, _ = validated
    numbers = (
        linked_message_numbers(message_infos(text), number, depth)
        if with_links
        else [number]
    )
    for index, selected in enumerate(numbers):
        if index:
            print("\n---\n")
        label = "REQUESTED" if selected == number else "LINKED-PRIOR"
        print(f"[thread] {label} #{selected:04d}")
        print(message_block(text, selected))
    return 0


def awaiting_reply_messages(text: str) -> list[MessageInfo]:
    messages = message_infos(text)
    answered = {target for message in messages for target in message.reply_targets}
    return [
        message
        for message in messages
        if message.reply_required
        and message.status != "DONE"
        and message.number not in answered
    ]


def awaiting_reply_count(text: str) -> int:
    return len(awaiting_reply_messages(text))


def with_awaiting(output: str, text: str) -> str:
    return f"{output} awaiting_reply={awaiting_reply_count(text)}"


def validate_text(text: str, expected_last: int | None = None) -> tuple[bool, list[str], int]:
    problems: list[str] = []
    crlf, lf_only, stray_cr = newline_counts(text)
    if crlf and lf_only:
        problems.append(f"mixed line endings: crlf={crlf}, lf_only={lf_only}")
    # Standalone CR bytes can occur inside preserved historical message bodies.
    # Report their count, but reject only an actual CRLF/LF newline mixture.

    found = parse_headers(text)
    if not found:
        if expected_last not in (None, 0):
            problems.append(f"expected last #{expected_last:04d}, got #{0:04d}")
        return not problems, problems, 0

    seen: dict[int, list[int]] = {}
    for number, line_no in found:
        seen.setdefault(number, []).append(line_no)
    for number, line_numbers in sorted(seen.items()):
        if len(line_numbers) > 1:
            problems.append(f"duplicate message #{number:04d}: lines {line_numbers}")

    numbers = [number for number, _ in found]
    for previous, current in zip(numbers, numbers[1:]):
        if current <= previous:
            problems.append(f"message order inversion: #{previous:04d} then #{current:04d}")
    last = numbers[-1]
    if last != max(numbers):
        problems.append(f"last message #{last:04d} is not maximum #{max(numbers):04d}")
    if expected_last is not None and last != expected_last:
        problems.append(f"expected last #{expected_last:04d}, got #{last:04d}")

    lines = text.splitlines(keepends=True)
    for index, (number, line_no) in enumerate(found):
        start = line_no - 1
        end = found[index + 1][1] - 1 if index + 1 < len(found) else len(lines)
        state_rows = metadata_state_lines(lines, start, end)
        if len(state_rows) != 1:
            problems.append(f"message #{number:04d} has {len(state_rows)} metadata status rows")
            continue
        state_value = lines[state_rows[0]].rstrip("\r\n").removeprefix("- 상태: ")
        if state_value not in STATUSES:
            problems.append(f"message #{number:04d} has invalid status {state_value!r}")

        reply_rows = metadata_field_lines(lines, start, end, "- 답필요: ")
        if len(reply_rows) != 1:
            problems.append(f"message #{number:04d} has {len(reply_rows)} reply-required rows")
        else:
            reply_value = (
                lines[reply_rows[0]].rstrip("\r\n").removeprefix("- 답필요: ")
            )
            if reply_value not in ("Y", "N"):
                problems.append(
                    f"message #{number:04d} has invalid reply-required value {reply_value!r}"
                )

        target_rows = metadata_field_lines(lines, start, end, "- 회신대상: ")
        if len(target_rows) > 1:
            problems.append(f"message #{number:04d} has {len(target_rows)} reply-target rows")
        elif target_rows:
            value = (
                lines[target_rows[0]].rstrip("\r\n").removeprefix("- 회신대상: ")
            )
            try:
                targets = parse_structured_reply_targets(value)
            except ValueError as error:
                problems.append(f"message #{number:04d} has invalid reply targets: {error}")
            else:
                prior_numbers = {prior for prior, _ in found[:index]}
                for target in targets:
                    if target not in prior_numbers:
                        problems.append(
                            f"message #{number:04d} reply target #{target:04d} "
                            "must reference an existing earlier message"
                        )
    return not problems, problems, last


def validate(path: Path, expected_last: int | None = None) -> tuple[bool, list[str], int]:
    if not path.exists():
        return False, [f"thread not found: {path}"], 0
    return validate_raw(read_bytes(path), expected_last=expected_last)


def newline_summary(text: str) -> str:
    crlf, lf_only, stray_cr = newline_counts(text)
    return f"newline={newline_label(text)} crlf={crlf} lf_only={lf_only} stray_cr={stray_cr}"


@contextlib.contextmanager
def mutation_lock(path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialize helper mutations with an exclusive sidecar lock."""
    lock_path = path.with_name(f".{path.name}.mailbox.lock")
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for mutation lock: {lock_path}")
            time.sleep(0.05)
    try:
        os.write(descriptor, f"pid={os.getpid()} created={dt.datetime.now().isoformat()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write(path: Path, raw: bytes) -> None:
    """Replace a file from the same directory after flushing the candidate."""
    original_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if original_mode is not None:
            os.chmod(temporary_path, original_mode)
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "templates" / "thread-header.md"


def sanitize_mode_metadata(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"mode {field} must not be empty")
    if "\r" in value or "\n" in value:
        raise ValueError(f"mode {field} must not contain newlines")
    if "THREAD:MODE:" in value or "THREAD:PINNED:" in value:
        raise ValueError(f"mode {field} must not contain marker text")
    return cleaned


def formatted_mode_body(
    mode_value: str,
    setter: str,
    reason: str,
    newline: str,
    timestamp: str | None = None,
) -> bytes:
    if mode_value not in MODES:
        raise ValueError(f"invalid mode value {mode_value!r}; expected GIT or LOCAL")
    setter = sanitize_mode_metadata(setter, "setter")
    reason = sanitize_mode_metadata(reason, "reason")
    when = timestamp or dt.datetime.now().strftime("%Y-%m-%d %H:%M KST")
    body = (
        f"- 모드: {mode_value}{newline}"
        f"- 설정: {setter} · {when}{newline}"
        f"- 사유: {reason}{newline}"
    )
    return body.encode("utf-8")


def formatted_mode_block(
    mode_value: str,
    setter: str,
    reason: str,
    newline: str,
    timestamp: str | None = None,
) -> bytes:
    body = formatted_mode_body(mode_value, setter, reason, newline, timestamp)
    return (
        MODE_START.encode("ascii")
        + newline.encode("ascii")
        + body
        + MODE_END.encode("ascii")
        + newline.encode("ascii") * 2
    )


def init_thread(path: Path, title: str, mode_value: str | None = None) -> int:
    if path.exists():
        print(f"[thread] refusing to overwrite existing file: {path}", file=sys.stderr)
        return 2
    template = template_path()
    if not template.exists():
        print(f"[thread] template not found: {template}", file=sys.stderr)
        return 2
    content = template.read_text(encoding="utf-8").replace("{{TITLE}}", title)
    raw = content.encode("utf-8")
    if mode_value is not None:
        layout = thread_layout(raw)
        if layout.pin is None:
            print("[thread] init template has no pinned markers", file=sys.stderr)
            return 3
        newline = newline_sequence(content)
        block = formatted_mode_block(mode_value, "OWNER", "init --mode", newline)
        raw = raw[: layout.pin.start] + block + raw[layout.pin.start :]
    ok, problems, _ = validate_raw(raw)
    if not ok:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 3
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, raw)
    print(f"[thread] initialized -> {path}")
    return 0


def messages_with_status(path: Path, statuses: tuple[str, ...]) -> list[tuple[int, str, str]]:
    text = read_text(path)
    lines = text.splitlines(keepends=True)
    found = parse_headers(text)
    pending: list[tuple[int, str, str]] = []
    for index, (number, line_no) in enumerate(found):
        end = found[index + 1][1] - 1 if index + 1 < len(found) else len(lines)
        start = line_no - 1
        state_rows = metadata_state_lines(lines, start, end)
        if len(state_rows) != 1:
            continue
        state_value = lines[state_rows[0]].strip().removeprefix("- 상태: ")
        if state_value in statuses:
            pending.append((number, state_value, lines[start].rstrip("\r\n")))
    return pending


def enforce_expected_mode(raw: bytes, expected_mode: str | None, command: str) -> int:
    info = mode_info(raw)
    if info is None:
        if expected_mode is not None:
            print(
                f"[thread] {command} refused: --expect-mode was provided but no mode block exists",
                file=sys.stderr,
            )
            return 5
        return 0
    if expected_mode is None:
        print(
            f"[thread] {command} refused: mode block declares {info.value}; "
            "--expect-mode is required",
            file=sys.stderr,
        )
        return 5
    if expected_mode != info.value:
        print(
            f"[thread] {command} refused: expected mode {expected_mode}, "
            f"declared mode {info.value}",
            file=sys.stderr,
        )
        return 5
    print(f"[thread] mode={info.value}", file=sys.stderr)
    return 0


def resolve(path: Path, number: int, status_value: str, expected_mode: str | None) -> int:
    with mutation_lock(path):
        raw = read_bytes(path)
        text = decode_utf8(raw)
        ok, problems, _ = validate_raw(raw)
        if not ok:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 2
        mode_result = enforce_expected_mode(raw, expected_mode, "resolve")
        if mode_result:
            return mode_result
        lines = text.splitlines(keepends=True)
        found = parse_headers(text)
        index = next((i for i, item in enumerate(found) if item[0] == number), None)
        if index is None:
            print(f"[thread] message not found: #{number:04d}", file=sys.stderr)
            return 2
        start = found[index][1] - 1
        end = found[index + 1][1] - 1 if index + 1 < len(found) else len(lines)
        state_lines = metadata_state_lines(lines, start, end)
        if len(state_lines) != 1:
            print(f"[thread] #{number:04d} has {len(state_lines)} status lines; refusing", file=sys.stderr)
            return 2
        old_line = lines[state_lines[0]]
        newline = "\r\n" if old_line.endswith("\r\n") else "\n" if old_line.endswith("\n") else ""
        lines[state_lines[0]] = f"- 상태: {status_value}{newline}"
        candidate = "".join(lines)
        candidate_raw = candidate.encode("utf-8")
        ok, problems, _ = validate_raw(candidate_raw)
        if not ok:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 3
        atomic_write(path, candidate_raw)
    print(with_awaiting(f"[thread] resolved #{number:04d} -> {status_value}", candidate))
    return 0


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8").strip()
    if args.body:
        return args.body.replace("\r\n", "\n").strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("body required: --body-file, --body, or stdin")


def append(path: Path, args: argparse.Namespace) -> int:
    body = read_body(args)
    if len(body) > MAX_INLINE_BODY_CHARS:
        print(
            f"[thread] append refused: body has {len(body)} chars; default maximum is "
            f"{MAX_INLINE_BODY_CHARS}. Store the document in an artifact, then append a "
            "short reply that links both --artifact and --in-reply-to.",
            file=sys.stderr,
        )
        return 6
    with mutation_lock(path):
        existing_raw = read_bytes(path)
        existing = decode_utf8(existing_raw)
        ok, problems, last = validate_raw(existing_raw)
        if not ok:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 2
        mode_result = enforce_expected_mode(existing_raw, args.expect_mode, "append")
        if mode_result:
            return mode_result
        if args.expect_last is not None and last != args.expect_last:
            print(
                f"[thread] append refused: expected last #{args.expect_last:04d}, "
                f"actual last #{last:04d}; reread before retrying",
                file=sys.stderr,
            )
            return 5

        number = last + 1
        try:
            reply_targets = parse_cli_reply_targets(args.in_reply_to)
        except ValueError as error:
            print(f"[thread] append refused: {error}", file=sys.stderr)
            return 2
        artifacts = tuple(args.artifact or ())
        if artifacts and not reply_targets:
            print(
                "[thread] append refused: --artifact requires --in-reply-to so the "
                "artifact is connected to its target message",
                file=sys.stderr,
            )
            return 2
        for artifact in artifacts:
            if not artifact.strip() or "\n" in artifact or "\r" in artifact:
                print("[thread] append refused: artifact references must be one non-empty line", file=sys.stderr)
                return 2
        existing_numbers = {message_number for message_number, _ in parse_headers(existing)}
        for target in reply_targets:
            if target not in existing_numbers or target >= number:
                print(
                    f"[thread] append refused: reply target #{target:04d} must reference "
                    "an existing earlier message",
                    file=sys.stderr,
                )
                return 2

        timestamp = args.date or dt.datetime.now().strftime("%Y-%m-%d %H:%M KST")
        newline = newline_sequence(existing)
        reply_target_line = ""
        if reply_targets:
            formatted_targets = ",".join(f"#{target:04d}" for target in reply_targets)
            reply_target_line = f"- 회신대상: {formatted_targets}\n"
        artifact_lines = "".join(
            f"- 산출물: {artifact.strip()}\n" for artifact in artifacts
        )
        entry_lf = (
            f"### [{args.sender} → {args.receiver}] {timestamp} · {args.type} · "
            f"#{number:04d} · [{args.tag}] {args.title}\n"
            f"- 대상: {args.target}\n"
            f"- 답필요: {args.reply}\n"
            f"{reply_target_line}"
            f"{artifact_lines}"
            f"- 상태: {args.status}\n"
            "- 본문:\n"
            f"{body}\n"
        )
        entry = entry_lf if newline == "\n" else entry_lf.replace("\n", "\r\n")
        if not existing or existing.endswith(newline * 2):
            separator = ""
        elif existing.endswith(newline):
            separator = newline
        else:
            separator = newline * 2
        candidate = existing + separator + entry
        candidate_raw = candidate.encode("utf-8")
        ok, problems, candidate_last = validate_raw(candidate_raw, expected_last=number)
        if not ok or candidate_last != number:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 3
        atomic_write(path, candidate_raw)
    print(with_awaiting(f"[thread] appended #{number:04d} -> {path}", candidate))
    return 0


def pin_hash(raw: bytes) -> str:
    layout = thread_layout(raw)
    if layout.pin is None:
        raise ValueError("pin requires markers in the pre-message region")
    return hashlib.sha256(layout.pin.body).hexdigest()


def marker_block_bytes(raw: bytes, region: MarkerRegion | None) -> bytes:
    if region is None:
        return b""
    return raw[region.start : region.end_after]


def pin(path: Path, args: argparse.Namespace) -> int:
    if args.show_hash:
        raw = read_bytes(path)
        ok, problems, _ = validate_raw(raw)
        if not ok:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 2
        mode_result = enforce_expected_mode(raw, args.expect_mode, "pin")
        if mode_result:
            return mode_result
        try:
            current_hash = pin_hash(raw)
        except ValueError as error:
            print(f"[thread] pin refused: {error}", file=sys.stderr)
            return 2
        print(with_awaiting(f"[thread] pin-hash {current_hash}", decode_utf8(raw)))
        return 0

    if not args.expect_hash:
        print("[thread] pin update requires --expect-hash", file=sys.stderr)
        return 2
    body = read_body(args)
    with mutation_lock(path):
        raw = read_bytes(path)
        text = decode_utf8(raw)
        ok, problems, _ = validate_raw(raw)
        if not ok:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 2
        mode_result = enforce_expected_mode(raw, args.expect_mode, "pin")
        if mode_result:
            return mode_result
        try:
            layout_before = thread_layout(raw)
            if layout_before.pin is None:
                raise ValueError("pin requires markers in the pre-message region")
            body_start = layout_before.pin.body_start
            body_end = layout_before.pin.body_end
            old_body = layout_before.pin.body
        except ValueError as error:
            print(f"[thread] pin refused: {error}", file=sys.stderr)
            return 2
        current_hash = hashlib.sha256(old_body).hexdigest()
        if current_hash != args.expect_hash:
            print(
                f"[thread] pin refused: expected hash {args.expect_hash}, "
                f"actual hash {current_hash}",
                file=sys.stderr,
            )
            return 5

        newline = newline_sequence(text)
        normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if newline == "\r\n":
            normalized_body = normalized_body.replace("\n", "\r\n")
        replacement = (normalized_body + newline).encode("utf-8")
        candidate_raw = raw[:body_start] + replacement + raw[body_end:]
        try:
            layout_after = thread_layout(candidate_raw)
        except ValueError as error:
            print(f"[thread] pin refused: {error}", file=sys.stderr)
            return 3
        mode_before = marker_block_bytes(raw, layout_before.mode)
        mode_after = marker_block_bytes(candidate_raw, layout_after.mode)
        messages_before = raw[layout_before.first_message :]
        messages_after = candidate_raw[layout_after.first_message :]
        if mode_after != mode_before:
            print("[thread] pin refused: mode bytes would change", file=sys.stderr)
            return 3
        if messages_after != messages_before:
            print("[thread] pin refused: message bytes would change", file=sys.stderr)
            return 3
        ok, problems, _ = validate_raw(candidate_raw)
        if not ok:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 3
        atomic_write(path, candidate_raw)
        updated_hash = pin_hash(candidate_raw)
    print(
        with_awaiting(
            f"[thread] pin updated hash={updated_hash}", decode_utf8(candidate_raw)
        )
    )
    return 0


def show_mode(path: Path, args: argparse.Namespace) -> int:
    if any((args.set_mode, args.by, args.reason, args.expect_current_mode, args.expect_hash)):
        print("[thread] mode --show does not accept set-only arguments", file=sys.stderr)
        return 2
    raw = read_bytes(path)
    ok, problems, _ = validate_raw(raw)
    if not ok:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    try:
        info = mode_info(raw, reject_message_only_markers=True)
    except ValueError as error:
        print(f"[thread] mode refused: {error}", file=sys.stderr)
        return 2
    if info is None:
        print(with_awaiting("[thread] mode UNDECLARED", decode_utf8(raw)))
        return 0
    print(
        with_awaiting(
            f"[thread] mode {info.value} hash={info.body_hash} "
            f"by={info.setter!r} reason={info.reason!r}",
            decode_utf8(raw),
        )
    )
    return 0


def set_mode(path: Path, args: argparse.Namespace) -> int:
    if not args.by or not args.reason:
        print("[thread] mode --set requires --by and --reason", file=sys.stderr)
        return 2
    if args.expect_current_mode and args.expect_hash:
        print(
            "[thread] mode --set accepts only one of --expect-current-mode or --expect-hash",
            file=sys.stderr,
        )
        return 2
    try:
        setter = sanitize_mode_metadata(args.by, "setter")
        reason = sanitize_mode_metadata(args.reason, "reason")
    except ValueError as error:
        print(f"[thread] mode refused: {error}", file=sys.stderr)
        return 2

    with mutation_lock(path):
        raw = read_bytes(path)
        ok, problems, _ = validate_raw(raw)
        if not ok:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 2
        try:
            layout_before = thread_layout(raw)
            current = mode_info(raw, reject_message_only_markers=True)
        except ValueError as error:
            print(f"[thread] mode refused: {error}", file=sys.stderr)
            return 2

        if current is None:
            if args.expect_current_mode is not None or args.expect_hash is not None:
                print(
                    "[thread] mode set refused: stale guard was provided but no mode block exists",
                    file=sys.stderr,
                )
                return 5
            if layout_before.pin is None:
                print(
                    "[thread] mode set refused: markerless pinned context requires a dedicated migration",
                    file=sys.stderr,
                )
                return 2
        else:
            if args.expect_current_mode is None and args.expect_hash is None:
                print(
                    "[thread] mode set refused: existing block requires "
                    "--expect-current-mode or --expect-hash",
                    file=sys.stderr,
                )
                return 5
            if args.expect_current_mode is not None and args.expect_current_mode != current.value:
                print(
                    f"[thread] mode set refused: expected current mode "
                    f"{args.expect_current_mode}, actual {current.value}",
                    file=sys.stderr,
                )
                return 5
            if args.expect_hash is not None and args.expect_hash != current.body_hash:
                print(
                    f"[thread] mode set refused: expected hash {args.expect_hash}, "
                    f"actual {current.body_hash}",
                    file=sys.stderr,
                )
                return 5

        text = decode_utf8(raw)
        newline = newline_sequence(text)
        replacement_body = formatted_mode_body(args.set_mode, setter, reason, newline)
        if layout_before.mode is None:
            assert layout_before.pin is not None
            block = formatted_mode_block(args.set_mode, setter, reason, newline)
            insert_at = layout_before.pin.start
            candidate_raw = raw[:insert_at] + block + raw[insert_at:]
        else:
            candidate_raw = (
                raw[: layout_before.mode.body_start]
                + replacement_body
                + raw[layout_before.mode.body_end :]
            )

        try:
            layout_after = thread_layout(candidate_raw)
            updated = mode_info(candidate_raw)
        except ValueError as error:
            print(f"[thread] mode refused: {error}", file=sys.stderr)
            return 3
        if updated is None or updated.value != args.set_mode:
            print("[thread] mode refused: candidate mode validation failed", file=sys.stderr)
            return 3
        pin_before = marker_block_bytes(raw, layout_before.pin)
        pin_after = marker_block_bytes(candidate_raw, layout_after.pin)
        messages_before = raw[layout_before.first_message :]
        messages_after = candidate_raw[layout_after.first_message :]
        if pin_after != pin_before:
            print("[thread] mode refused: pinned bytes would change", file=sys.stderr)
            return 3
        if messages_after != messages_before:
            print("[thread] mode refused: message bytes would change", file=sys.stderr)
            return 3
        ok, problems, _ = validate_raw(candidate_raw)
        if not ok:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 3
        atomic_write(path, candidate_raw)

    print(
        with_awaiting(
            f"[thread] mode updated mode={updated.value} hash={updated.body_hash}",
            decode_utf8(candidate_raw),
        )
    )
    return 0


def mode_command(path: Path, args: argparse.Namespace) -> int:
    if args.show:
        return show_mode(path, args)
    return set_mode(path, args)


def build_parser() -> argparse.ArgumentParser:
    parser = ThreadArgumentParser(description="Operate a collaboration mailbox thread")
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    parser.add_argument("--thread", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("init")
    create.add_argument("--title", default="Claude/Codex Collaboration Thread")
    create.add_argument("--mode", choices=MODES, default=None)
    sub.add_parser("validate")
    opened = sub.add_parser("open")
    opened_selection = opened.add_mutually_exclusive_group()
    opened_selection.add_argument(
        "--all",
        action="store_true",
        help="include REVIEW and BLOCKED with OPEN; status selection may expand in a later version",
    )
    opened_selection.add_argument(
        "--awaiting",
        action="store_true",
        help="show reply-required messages with no explicit reply connection",
    )
    opened.add_argument(
        "--agent",
        default=None,
        help="with --awaiting, match this exact receiver token or ALL",
    )
    opened.add_argument(
        "--after-number",
        type=int,
        default=None,
        help="with --awaiting, include only message numbers greater than this value",
    )
    sub.add_parser("next")

    intake_parser = sub.add_parser(
        "intake", help="validate and emit one bounded pinned/pending/recent snapshot"
    )
    intake_parser.add_argument("--agent", default="CODEX")
    intake_parser.add_argument("--limit", type=int, default=3)
    intake_parser.add_argument("--recent", type=int, default=1)
    intake_parser.add_argument("--body-chars", type=int, default=3000)
    intake_parser.add_argument("--pinned-chars", type=int, default=2000)
    intake_parser.add_argument("--no-pinned", action="store_true")
    intake_parser.add_argument(
        "--known-pinned-hash",
        default=None,
        help="compare the current PINNED hash with a previously observed full SHA-256",
    )
    intake_parser.add_argument(
        "--known-last",
        type=int,
        default=None,
        help="emit relevant messages after the previously observed last number",
    )

    shown_message = sub.add_parser("show", help="print exactly one numbered message")
    shown_message.add_argument("--number", type=int, required=True)
    shown_message.add_argument(
        "--with-links",
        action="store_true",
        help="also print structured earlier reply targets",
    )
    shown_message.add_argument("--depth", type=int, default=3)

    change = sub.add_parser("resolve")
    change.add_argument("--number", type=int, required=True)
    change.add_argument("--status", choices=STATUSES[1:], required=True)
    change.add_argument("--expect-mode", choices=MODES, default=None)

    add = sub.add_parser("append")
    add.add_argument("--sender", default="CODEX")
    add.add_argument("--receiver", default="CLAUDE/OWNER")
    add.add_argument("--type", choices=TYPES, default="REPORT")
    add.add_argument("--tag", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--target", default="Claude / Owner")
    add.add_argument("--reply", choices=("Y", "N"), required=True, metavar="{Y,N}")
    add.add_argument("--status", choices=STATUSES, default="OPEN")
    add.add_argument("--date", default=None)
    add.add_argument("--body-file", default=None)
    add.add_argument("--body", default=None)
    add.add_argument("--expect-last", type=int, default=None)
    add.add_argument("--expect-mode", choices=MODES, default=None)
    add.add_argument(
        "--in-reply-to",
        default=None,
        help="comma-separated earlier message numbers, for example 222,223",
    )
    add.add_argument(
        "--artifact",
        action="append",
        default=None,
        help="artifact path or URL; requires --in-reply-to",
    )

    pinned = sub.add_parser("pin")
    pinned.add_argument("--show-hash", action="store_true")
    pinned.add_argument("--expect-hash", default=None)
    pinned.add_argument("--body-file", default=None)
    pinned.add_argument("--body", default=None)
    pinned.add_argument("--expect-mode", choices=MODES, default=None)

    mode_parser = sub.add_parser("mode")
    mode_action = mode_parser.add_mutually_exclusive_group(required=True)
    mode_action.add_argument("--show", action="store_true")
    mode_action.add_argument("--set", dest="set_mode", choices=MODES)
    mode_parser.add_argument("--by", default=None)
    mode_parser.add_argument("--reason", default=None)
    mode_parser.add_argument("--expect-current-mode", choices=MODES, default=None)
    mode_parser.add_argument("--expect-hash", default=None)
    return parser


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass
    args = build_parser().parse_args()
    path = args.thread.resolve()
    if args.command == "init":
        return init_thread(path, args.title, args.mode)
    if args.command == "validate":
        ok, problems, last = validate(path)
        text = read_text(path) if path.exists() else ""
        if ok:
            count = len(parse_headers(text))
            raw = read_bytes(path)
            info = mode_info(raw)
            mode_label = info.value if info is not None else "UNDECLARED"
            print(
                with_awaiting(
                    f"[thread] ok messages={count} last=#{last:04d} "
                    f"{newline_summary(text)} mode={mode_label}",
                    text,
                )
            )
            return 0
        for problem in problems:
            print(problem, file=sys.stderr)
        if path.exists():
            print(f"[thread] {newline_summary(text)}", file=sys.stderr)
        return 1
    if args.command == "open":
        text = read_text(path)
        if args.awaiting:
            awaiting = awaiting_reply_messages(text)
            if args.after_number is not None and args.after_number < 0:
                print(
                    "[thread] open refused: --after-number must be 0 or greater",
                    file=sys.stderr,
                )
                return 2
            awaiting = [
                message
                for message in awaiting
                if receiver_matches(message.receiver, args.agent)
                and (
                    args.after_number is None
                    or message.number > args.after_number
                )
            ]
            if args.agent is not None or args.after_number is not None:
                after_label = (
                    f"#{args.after_number:04d}"
                    if args.after_number is not None
                    else "NONE"
                )
                print(
                    f"[thread] awaiting agent={args.agent or 'ALL'} "
                    f"after={after_label} matched={len(awaiting)}"
                )
            if not awaiting:
                print(with_awaiting("[thread] no awaiting reply messages", text))
            for message in awaiting:
                print(
                    with_awaiting(
                        f"#{message.number:04d} · {message.sender} → "
                        f"{message.receiver} · {message.title}",
                        text,
                    )
                )
            return 0
        if args.agent is not None or args.after_number is not None:
            print(
                "[thread] open refused: --agent and --after-number require --awaiting",
                file=sys.stderr,
            )
            return 2
        statuses = PENDING_STATUSES if args.all else ("OPEN",)
        pending = messages_with_status(path, statuses)
        if not pending:
            label = "pending" if args.all else "OPEN"
            print(with_awaiting(f"[thread] no {label} messages", text))
        for number, status_value, header in pending:
            print(with_awaiting(f"#{number:04d} [{status_value}] {header}", text))
        return 0
    if args.command == "intake":
        return intake(path, args)
    if args.command == "show":
        return show_message(path, args.number, args.with_links, args.depth)
    if args.command == "next":
        ok, problems, last = validate(path)
        structural_problems = [
            problem for problem in problems if not problem.startswith("mixed line endings:")
        ]
        if structural_problems:
            for problem in structural_problems:
                print(problem, file=sys.stderr)
            return 1
        if not ok:
            print(
                "[thread] warning: mixed newlines detected; read-only next is allowed, "
                "mutation commands will refuse",
                file=sys.stderr,
            )
        print(f"{last + 1:04d}")
        return 0
    if args.command == "resolve":
        return resolve(path, args.number, args.status, args.expect_mode)
    if args.command == "pin":
        return pin(path, args)
    if args.command == "mode":
        return mode_command(path, args)
    return append(path, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UnicodeDecodeError as error:
        print(
            "[thread] invalid UTF-8 bytes detected; original file was not changed. "
            f"({error})",
            file=sys.stderr,
        )
        raise SystemExit(4)
    except (TimeoutError, ValueError) as error:
        print(f"[thread] safety check failed: {error}; original file was not changed.", file=sys.stderr)
        raise SystemExit(4)
