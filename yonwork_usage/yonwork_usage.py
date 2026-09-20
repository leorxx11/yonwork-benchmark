#!/usr/bin/env python3
"""Collect token usage for a serial YonWork benchmark run."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import time
import traceback
from typing import Any, BinaryIO, Sequence


STATE_DIR = Path(__file__).resolve().parent / ".state"
DEFAULT_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.5
SESSION_TYPES = ("normal", "cron", "team-chat", "weixin", "other")

JsonObject = dict[str, Any]
TokenNumber = int | float


class CollectorError(Exception):
    """An expected failure that can be returned as machine-readable JSON."""

    def __init__(self, error_code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details

    def as_result(self, benchmark_id: str | None = None) -> JsonObject:
        result: JsonObject = {"success": False}
        if benchmark_id is not None:
            result["benchmarkId"] = benchmark_id
        result["errorCode"] = self.error_code
        result["message"] = self.message
        result.update(self.details)
        return result


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep command-line errors on the JSON result path."""

    def error(self, message: str) -> None:
        raise CollectorError("INVALID_ARGUMENT", message)


def _validate_benchmark_id(benchmark_id: str) -> None:
    if not benchmark_id or benchmark_id in {".", ".."}:
        raise CollectorError("INVALID_BENCHMARK_ID", "benchmark-id must not be empty.")

    invalid_characters = '<>:"/\\|?*'
    if (
        benchmark_id.rstrip(" .") != benchmark_id
        or any(ord(character) < 32 or character in invalid_characters for character in benchmark_id)
    ):
        raise CollectorError(
            "INVALID_BENCHMARK_ID",
            "benchmark-id contains characters that cannot be used in a Windows state filename.",
        )


def _state_path(benchmark_id: str) -> Path:
    _validate_benchmark_id(benchmark_id)
    return STATE_DIR / f"{benchmark_id}.json"


def _resolve_log_path(log_path: str) -> Path:
    path = Path(log_path).expanduser()
    if not path.is_file():
        raise CollectorError(
            "LOG_FILE_NOT_FOUND",
            f"Log file was not found: {path}",
        )
    try:
        return path.resolve()
    except OSError as exc:
        raise CollectorError(
            "LOG_FILE_NOT_FOUND",
            f"Log file could not be resolved: {path}",
        ) from exc


def begin_collection(benchmark_id: str, log_path: str) -> JsonObject:
    """Record the current byte offset of an explicitly supplied log file."""

    state_path = _state_path(benchmark_id)
    resolved_log_path = _resolve_log_path(log_path)
    try:
        offset = resolved_log_path.stat().st_size
    except OSError as exc:
        raise CollectorError(
            "LOG_FILE_NOT_FOUND",
            f"Log file could not be read: {resolved_log_path}",
        ) from exc

    state: JsonObject = {
        "benchmarkId": benchmark_id,
        "logPath": str(resolved_log_path),
        "offset": offset,
        "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise CollectorError(
            "STATE_WRITE_FAILED",
            f"State file could not be written: {state_path}",
        ) from exc

    return {
        "success": True,
        "benchmarkId": benchmark_id,
        "offset": offset,
    }


def _load_state(benchmark_id: str) -> JsonObject:
    state_path = _state_path(benchmark_id)
    if not state_path.is_file():
        raise CollectorError(
            "STATE_NOT_FOUND",
            f"State file was not found for benchmark-id {benchmark_id}: {state_path}",
        )

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollectorError(
            "STATE_INVALID",
            f"State file is not valid JSON: {state_path}",
        ) from exc

    if not isinstance(state, dict):
        raise CollectorError("STATE_INVALID", f"State file must contain a JSON object: {state_path}")
    if state.get("benchmarkId") != benchmark_id:
        raise CollectorError("STATE_INVALID", "State benchmarkId does not match the requested benchmark-id.")
    if not isinstance(state.get("logPath"), str) or not state["logPath"]:
        raise CollectorError("STATE_INVALID", "State logPath is missing or invalid.")
    offset = state.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise CollectorError("STATE_INVALID", "State offset is missing or invalid.")
    return state


def _parse_complete_line(raw_line: bytes, byte_offset: int) -> JsonObject | None:
    raw_line = raw_line.rstrip(b"\r")
    if not raw_line.strip():
        return None

    encoding = "utf-8-sig" if byte_offset == 0 else "utf-8"
    try:
        value = json.loads(raw_line.decode(encoding))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError(
            "INVALID_JSONL",
            f"Invalid JSONL record beginning at byte {byte_offset}.",
        ) from exc

    if not isinstance(value, dict):
        raise CollectorError(
            "INVALID_JSONL",
            f"JSONL record beginning at byte {byte_offset} is not a JSON object.",
        )
    return value


def _try_parse_tail(raw_line: bytes, byte_offset: int) -> tuple[bool, JsonObject | None]:
    """Parse a complete final line, but retain malformed text as a possible partial write."""

    if not raw_line.strip():
        return False, None

    encoding = "utf-8-sig" if byte_offset == 0 else "utf-8"
    try:
        value = json.loads(raw_line.decode(encoding))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None

    if not isinstance(value, dict):
        raise CollectorError(
            "INVALID_JSONL",
            f"JSONL record beginning at byte {byte_offset} is not a JSON object.",
        )
    return True, value


class IncrementalJsonlReader:
    """Read complete JSONL records once while retaining a concurrently written tail."""

    def __init__(self, log_path: Path, offset: int = 0) -> None:
        try:
            self._handle: BinaryIO = log_path.open("rb")
        except FileNotFoundError as exc:
            raise CollectorError("LOG_FILE_NOT_FOUND", f"Log file was not found: {log_path}") from exc
        except OSError as exc:
            raise CollectorError("LOG_READ_FAILED", f"Log file could not be opened: {log_path}") from exc

        try:
            self._handle.seek(0, 2)
            size = self._handle.tell()
            if offset > size:
                raise CollectorError(
                    "LOG_TRUNCATED",
                    f"State offset {offset} is beyond the current log size {size}.",
                )

            self._discard_initial_fragment = False
            if offset > 0:
                self._handle.seek(offset - 1)
                self._discard_initial_fragment = self._handle.read(1) != b"\n"
            self._handle.seek(offset)
        except Exception:
            self._handle.close()
            raise

        self._buffer = bytearray()
        self._buffer_offset = offset

    def __enter__(self) -> IncrementalJsonlReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._handle.close()

    def read_available(self) -> list[JsonObject]:
        try:
            chunk = self._handle.read()
        except OSError as exc:
            raise CollectorError("LOG_READ_FAILED", "The log file could not be read.") from exc

        if chunk:
            self._buffer.extend(chunk)

        if self._discard_initial_fragment:
            newline_index = self._buffer.find(b"\n")
            if newline_index < 0:
                self._buffer_offset += len(self._buffer)
                self._buffer.clear()
                return []
            del self._buffer[: newline_index + 1]
            self._buffer_offset += newline_index + 1
            self._discard_initial_fragment = False

        records: list[JsonObject] = []
        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index < 0:
                break

            line_offset = self._buffer_offset
            raw_line = bytes(self._buffer[:newline_index])
            del self._buffer[: newline_index + 1]
            self._buffer_offset += newline_index + 1
            record = _parse_complete_line(raw_line, line_offset)
            if record is not None:
                records.append(record)

        if self._buffer:
            parsed, record = _try_parse_tail(bytes(self._buffer), self._buffer_offset)
            if parsed:
                self._buffer_offset += len(self._buffer)
                self._buffer.clear()
                if record is not None:
                    records.append(record)

        return records


def _workspace_is_default(workspace_dir: Any) -> bool:
    if not isinstance(workspace_dir, str):
        return False
    normalized = workspace_dir.replace("\\", "/").rstrip("/").casefold()
    return normalized.endswith("userdata/workspaces/default")


def classify_session(record: JsonObject) -> str:
    """Classify a log record into the inspect categories."""

    session_key_value = record.get("sessionKey")
    session_key = session_key_value.casefold() if isinstance(session_key_value, str) else ""
    run_id_value = record.get("runId")
    run_id = run_id_value.casefold() if isinstance(run_id_value, str) else ""

    if ":cron:" in session_key:
        return "cron"
    if run_id.startswith("team-chat-summary:") or "team-personal" in session_key:
        return "team-chat"
    if "openclaw-weixin" in session_key:
        return "weixin"
    if record.get("agentId") == "main" and _workspace_is_default(record.get("workspaceDir")):
        return "normal"
    return "other"


def _is_target_input(record: JsonObject) -> bool:
    return record.get("event") == "llm_input" and classify_session(record) == "normal"


def _required_token(usage: JsonObject, key: str, run_id: Any) -> TokenNumber:
    value = usage.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise CollectorError(
            "USAGE_NOT_FOUND",
            f"Required output.usage.{key} was not found or was not a valid token count.",
            runId=run_id,
        )
    return value


def _optional_token(usage: JsonObject | None, key: str, run_id: Any) -> TokenNumber | None:
    if usage is None or key not in usage or usage[key] is None:
        return None
    value = usage[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise CollectorError(
            "USAGE_NOT_FOUND",
            f"Optional output.lastAssistant.usage.{key} was present but was not a valid token count.",
            runId=run_id,
        )
    return value


def _collection_result(
    benchmark_id: str,
    input_record: JsonObject,
    output_record: JsonObject,
) -> JsonObject:
    run_id = input_record.get("runId")
    output = output_record.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("usage"), dict):
        raise CollectorError(
            "USAGE_NOT_FOUND",
            "Required output.usage was not found on the matched llm_output record.",
            runId=run_id,
        )

    run_usage = output["usage"]
    run_input_tokens = _required_token(run_usage, "input", run_id)
    run_output_tokens = _required_token(run_usage, "output", run_id)
    run_total_tokens = _required_token(run_usage, "total", run_id)

    last_assistant = output.get("lastAssistant")
    final_usage_value = last_assistant.get("usage") if isinstance(last_assistant, dict) else None
    final_usage = final_usage_value if isinstance(final_usage_value, dict) else None
    final_input_tokens = _optional_token(final_usage, "input", run_id)
    final_output_tokens = _optional_token(final_usage, "output", run_id)
    final_total_tokens = _optional_token(final_usage, "totalTokens", run_id)

    token_amplification: float | None = None
    if final_total_tokens is not None and final_total_tokens != 0:
        token_amplification = round(run_total_tokens / final_total_tokens, 2)

    return {
        "success": True,
        "benchmarkId": benchmark_id,
        "runId": run_id,
        "sessionId": input_record.get("sessionId"),
        "provider": input_record.get("provider"),
        "model": input_record.get("model"),
        "harness": output.get("harnessId"),
        "runInputTokens": run_input_tokens,
        "runOutputTokens": run_output_tokens,
        "runTotalTokens": run_total_tokens,
        "finalInputTokens": final_input_tokens,
        "finalOutputTokens": final_output_tokens,
        "finalTotalTokens": final_total_tokens,
        "tokenAmplification": token_amplification,
        "inputTs": input_record.get("ts"),
        "outputTs": output_record.get("ts"),
    }


def _format_seconds(seconds: float) -> str:
    return f"{seconds:g}"


def collect_usage(
    benchmark_id: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> JsonObject:
    """Find the first eligible input after begin and its exact-run output."""

    if not math.isfinite(timeout) or timeout < 0:
        raise CollectorError("INVALID_ARGUMENT", "timeout must be a finite number greater than or equal to 0.")
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise CollectorError("INVALID_ARGUMENT", "poll interval must be a finite number greater than 0.")

    state = _load_state(benchmark_id)
    log_path = _resolve_log_path(state["logPath"])
    offset = state["offset"]
    deadline = time.monotonic() + timeout
    input_record: JsonObject | None = None

    with IncrementalJsonlReader(log_path, offset) as reader:
        while True:
            for record in reader.read_available():
                if input_record is None:
                    if _is_target_input(record):
                        input_record = record
                    continue

                if (
                    record.get("event") == "llm_output"
                    and record.get("runId") == input_record.get("runId")
                ):
                    return _collection_result(benchmark_id, input_record, record)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))

    if input_record is None:
        raise CollectorError(
            "INPUT_NOT_FOUND",
            "No ordinary main-session llm_input was found after the saved byte offset "
            f"within {_format_seconds(timeout)} seconds.",
        )

    run_id = input_record.get("runId")
    raise CollectorError(
        "OUTPUT_TIMEOUT",
        "Matched llm_input but no llm_output with the same runId was found within "
        f"{_format_seconds(timeout)} seconds.",
        runId=run_id,
    )


def inspect_log(log_path: str) -> JsonObject:
    """Read a complete explicitly supplied log and summarize input/output pairing."""

    resolved_log_path = _resolve_log_path(log_path)
    input_run_ids: set[str] = set()
    output_run_ids: set[str] = set()
    run_types: dict[str, str] = {}
    llm_input_count = 0
    llm_output_count = 0

    with IncrementalJsonlReader(resolved_log_path) as reader:
        records = reader.read_available()

    for record in records:
        event = record.get("event")
        if event not in {"llm_input", "llm_output"}:
            continue

        run_id_value = record.get("runId")
        run_id = run_id_value if isinstance(run_id_value, str) and run_id_value else None
        record_type = classify_session(record)

        if event == "llm_input":
            llm_input_count += 1
            if run_id is not None:
                input_run_ids.add(run_id)
                run_types[run_id] = record_type
        else:
            llm_output_count += 1
            if run_id is not None:
                output_run_ids.add(run_id)
                if run_id not in run_types or run_types[run_id] == "other":
                    run_types[run_id] = record_type

    all_run_ids = input_run_ids | output_run_ids
    type_counter = Counter(run_types[run_id] for run_id in all_run_ids)
    return {
        "success": True,
        "llmInputCount": llm_input_count,
        "llmOutputCount": llm_output_count,
        "uniqueRunIds": len(all_run_ids),
        "pairedRunCount": len(input_run_ids & output_run_ids),
        "unpairedInputRunIds": sorted(input_run_ids - output_run_ids),
        "unpairedOutputRunIds": sorted(output_run_ids - input_run_ids),
        "typeCounts": {session_type: type_counter[session_type] for session_type in SESSION_TYPES},
    }


def _build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(description="Collect YonWork benchmark token usage from an explicit JSONL log.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    begin_parser = subparsers.add_parser("begin", help="Save the current log byte offset.")
    begin_parser.add_argument("--benchmark-id", required=True)
    begin_parser.add_argument("--log-path", required=True)

    collect_parser = subparsers.add_parser("collect", help="Collect usage after the saved byte offset.")
    collect_parser.add_argument("--benchmark-id", required=True)
    collect_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)

    inspect_parser = subparsers.add_parser("inspect", help="Summarize an explicitly supplied JSONL log.")
    inspect_parser.add_argument("--log-path", required=True)
    return parser


def _benchmark_id_from_arguments(arguments: Sequence[str]) -> str | None:
    try:
        index = arguments.index("--benchmark-id")
    except ValueError:
        return None
    return arguments[index + 1] if index + 1 < len(arguments) else None


def _write_result(result: JsonObject) -> None:
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    benchmark_id = _benchmark_id_from_arguments(arguments)
    try:
        args = _build_parser().parse_args(arguments)
        benchmark_id = getattr(args, "benchmark_id", benchmark_id)
        if args.command == "begin":
            result = begin_collection(args.benchmark_id, args.log_path)
        elif args.command == "collect":
            result = collect_usage(args.benchmark_id, args.timeout)
        else:
            result = inspect_log(args.log_path)
    except CollectorError as exc:
        print(f"{exc.error_code}: {exc.message}", file=sys.stderr)
        _write_result(exc.as_result(benchmark_id))
        return 1
    except Exception:
        traceback.print_exc(file=sys.stderr)
        _write_result(
            CollectorError(
                "INTERNAL_ERROR",
                "An unexpected internal error occurred. See stderr for details.",
            ).as_result(benchmark_id)
        )
        return 1

    _write_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
