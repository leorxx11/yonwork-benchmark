#!/usr/bin/env python3
"""Extract an Electron .asar archive, or grep its sources, without needing Node.

用法:
    python extract_asar.py list    D:\\yonwork\\resources\\app.asar
    python extract_asar.py extract D:\\yonwork\\resources\\app.asar -o D:\\yonwork-asar
    python extract_asar.py grep    D:\\yonwork\\resources\\app.asar -p devTools -p remote-debugging
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import struct
import sys
from typing import Any, Iterator

# 只在这些后缀里做 grep，避免把 122MB 的二进制资源也扫一遍
TEXT_SUFFIXES = {".js", ".mjs", ".cjs", ".json", ".ts", ".map", ".html", ".yml", ".yaml"}

# 默认搜索的 dev 开关线索
DEFAULT_PATTERNS = [
    "devTools",
    "remote-debugging",
    "inspect-brk",
    "openDevTools",
    "NODE_ENV",
    "isDev",
    "isDevelopment",
    "appendSwitch",
    "commandLine",
]


def read_header(fp) -> tuple[dict[str, Any], int]:
    """Return (header dict, base offset of the file payload)."""
    prefix = fp.read(8)
    if len(prefix) < 8:
        raise ValueError("文件过短，不是合法的 asar")
    _pickle_size, header_size = struct.unpack("<II", prefix)
    header_buf = fp.read(header_size)
    (json_len,) = struct.unpack("<I", header_buf[4:8])
    header_json = header_buf[8 : 8 + json_len].decode("utf-8")
    return json.loads(header_json), header_size + 8


def walk(node: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    for name, entry in node.get("files", {}).items():
        path = f"{prefix}/{name}" if prefix else name
        if "files" in entry:
            yield from walk(entry, path)
        else:
            yield path, entry


def entry_bytes(fp, base: int, entry: dict[str, Any], asar_path: Path) -> bytes | None:
    """Read one file's bytes; unpacked entries live beside the archive."""
    if entry.get("unpacked"):
        return None
    offset = base + int(entry["offset"])
    fp.seek(offset)
    return fp.read(int(entry["size"]))


def cmd_list(args) -> int:
    asar = Path(args.archive)
    with asar.open("rb") as fp:
        header, _ = read_header(fp)
        rows = sorted(walk(header), key=lambda kv: -int(kv[1].get("size", 0)))
    print(f"{len(rows)} 个文件")
    for path, entry in rows[: args.limit]:
        flag = " (unpacked)" if entry.get("unpacked") else ""
        print(f"  {int(entry.get('size', 0)):>12,}  {path}{flag}")
    return 0


def cmd_extract(args) -> int:
    asar = Path(args.archive)
    out = Path(args.out)
    unpacked_dir = asar.with_name(asar.name + ".unpacked")
    written = skipped = 0
    with asar.open("rb") as fp:
        header, base = read_header(fp)
        for path, entry in walk(header):
            target = out / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.get("unpacked"):
                source = unpacked_dir / path
                if source.exists():
                    target.write_bytes(source.read_bytes())
                    written += 1
                else:
                    skipped += 1
                continue
            target.write_bytes(entry_bytes(fp, base, entry, asar) or b"")
            written += 1
    print(f"解出 {written} 个文件到 {out}" + (f"，{skipped} 个 unpacked 文件缺失" if skipped else ""))
    return 0


def cmd_grep(args) -> int:
    asar = Path(args.archive)
    patterns = args.pattern or DEFAULT_PATTERNS
    regexes = [(p, re.compile(re.escape(p), re.IGNORECASE)) for p in patterns]
    counts: dict[str, int] = {p: 0 for p in patterns}
    shown = 0
    with asar.open("rb") as fp:
        header, base = read_header(fp)
        main_entry = None
        for path, entry in walk(header):
            if path == "package.json":
                raw = entry_bytes(fp, base, entry, asar)
                if raw:
                    try:
                        pkg = json.loads(raw)
                        main_entry = pkg.get("main")
                        print(f"package.json main = {main_entry}")
                        print(f"package.json name = {pkg.get('name')}  version = {pkg.get('version')}\n")
                    except json.JSONDecodeError:
                        pass
                break

        fp.seek(0)
        header, base = read_header(fp)
        for path, entry in walk(header):
            if Path(path).suffix.lower() not in TEXT_SUFFIXES:
                continue
            if int(entry.get("size", 0)) > args.max_bytes:
                continue
            raw = entry_bytes(fp, base, entry, asar)
            if raw is None:
                continue
            try:
                text = raw.decode("utf-8", errors="ignore")
            except Exception:
                continue
            for label, rx in regexes:
                for m in rx.finditer(text):
                    counts[label] += 1
                    if shown < args.limit:
                        line_no = text.count("\n", 0, m.start()) + 1
                        start = max(0, m.start() - args.context)
                        end = min(len(text), m.end() + args.context)
                        snippet = text[start:end].replace("\n", "\\n")
                        print(f"{path}:{line_no}: ...{snippet}...")
                        shown += 1
    print("\n命中统计:")
    for label in patterns:
        print(f"  {counts[label]:>6}  {label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="按体积列出归档内文件")
    p_list.add_argument("archive")
    p_list.add_argument("--limit", type=int, default=40)
    p_list.set_defaults(func=cmd_list)

    p_ext = sub.add_parser("extract", help="解包到目录")
    p_ext.add_argument("archive")
    p_ext.add_argument("-o", "--out", required=True)
    p_ext.set_defaults(func=cmd_extract)

    p_grep = sub.add_parser("grep", help="在源码里搜 dev 开关线索")
    p_grep.add_argument("archive")
    p_grep.add_argument("-p", "--pattern", action="append")
    p_grep.add_argument("--limit", type=int, default=60, help="最多打印多少条命中")
    p_grep.add_argument("--context", type=int, default=90, help="每条命中前后各取多少字符")
    p_grep.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024)
    p_grep.set_defaults(func=cmd_grep)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
