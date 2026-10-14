#!/usr/bin/env python3
"""发布说明行尾预检：打 tag 前拦住会让 upload 阶段崩掉的坏字节。

问题
----
流水线 upload 阶段用 ``bianbu-dev`` 读 ``.changes``，一旦读到的不是合法
UTF-8 就抛 ``UnicodeDecodeError``：pack/build 都成功，只有 upload 失败
（tag ``bianbu/0.1.24`` 就是这样挂的，报 position 2100-2101）。

坏字节的来源是 dpkg。``Dpkg::Changelog::Entry`` 剥 changelog 每行行尾空白
用的是 Perl 的 ``s/\s+$//g``，而 Perl 按 Latin-1 逐字节扫，于是下面两个
字节也被当成空白吃掉：

* ``0xA0`` —— 大量 CJK 常用字的 UTF-8 末字节，如「加」``E5 8A A0``、
  「备」``E5 A4 87``、「期」``E6 9C 9F``；
* ``0x85`` —— 少见，但同属 Perl ``\s``。

一行若以这类字符结尾，末字节被吃掉后只剩 ``E5 8A`` 这样的前缀，不再是
合法 UTF-8。行尾再补空格没用（空格一样会被剥掉）；补任何一个非空白字符
（ASCII 标点或全角标点都行）即可保住。

用法
----
打 tag 前，检查准备写进 tag 注释的文本：

    python3 scripts/release_note_guard.py --tag-note /tmp/tag-note-0.1.25.txt

检查已存在 tag 的注释（需要 git）：

    python3 scripts/release_note_guard.py --tag bianbu/0.1.25

打包后，复核 CI 真正上传的 ``.changes``（第二道闸，能兜住 gbp 的任何重排）：

    python3 scripts/release_note_guard.py \
        --changes ../bianbu-agent-memory_0.1.25_riscv64.changes

也可以直接从标准输入读：``... --stdin < /tmp/tag-note.txt``。

退出码：0 = 干净；1 = 命中危险行。
"""
from __future__ import annotations

import argparse
import subprocess
import sys

# Perl \s 在字节视角下会剥掉的行尾字节：ASCII 惯用空白，外加 Latin-1 的
# NEL (0x85) 与 NBSP (0xA0)。已在本项目 K3（dpkg 1.23.7 / perl 5.40）上
# 逐字节验证：64 种合法 UTF-8 末字节里只有这两个会被吃掉。
TRAILING_BLANKS = frozenset({0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0})


def strip_perl_blanks(line: bytes) -> bytes:
    """模拟 ``s/[\\s]+$//`` 对一行字节的剥除。"""
    end = len(line)
    while end and line[end - 1] in TRAILING_BLANKS:
        end -= 1
    return line[:end]


def eaten_bytes(stripped: bytes) -> int:
    """返回末尾被 dpkg 吃掉的字节数（0 表示不是"缺前缀"型非法）。

    首字节决定这个字符本应多长，前缀缺几个字节就是被吃掉几个。
    """
    widths = {2: range(0xC0, 0xE0), 3: range(0xE0, 0xF0), 4: range(0xF0, 0xF8)}
    try:
        stripped.decode("utf-8")
        return 0
    except UnicodeDecodeError as error:
        if error.end != len(stripped) or not error.reason.startswith("unexpected end of data"):
            return 0
        lead = stripped[error.start]
        for width, span in widths.items():
            if lead in span:
                return width - (len(stripped) - error.start)
        return 0


def describe(stripped: bytes) -> str:
    """把剩下的坏行描述清楚：被 dpkg 吃掉了几个行尾字节。"""
    missing = eaten_bytes(stripped)
    if missing == 1:
        return ("行尾被吃掉 1 个字节（0xA0 最常见：CJK 常用字末字节，如「加」"
                "「备」；少数是 0x85），只剩不完整的前缀")
    if missing:
        return f"行尾被吃掉 {missing} 个字节，只剩不完整的前缀"
    return "行尾不是合法 UTF-8"


def scan(text: bytes) -> list[tuple[int, bytes]]:
    """返回会被剥成非法 UTF-8 的行：(1-based 行号, 去掉行尾空白后的字节)。"""
    hazards: list[tuple[int, bytes]] = []
    for index, raw in enumerate(text.split(b"\n"), start=1):
        line = raw[:-1] if raw.endswith(b"\r") else raw
        stripped = strip_perl_blanks(line)
        if not stripped:
            continue
        try:
            stripped.decode("utf-8")
        except UnicodeDecodeError:
            hazards.append((index, stripped))
    return hazards


def report(label: str, text: bytes) -> int:
    hazards = scan(text)
    if not hazards:
        print(f"OK: {label} 无危险行尾（{len(text.splitlines())} 行已检查）。")
        return 0
    print(f"FAIL: {label} 有 {len(hazards)} 行尾字符会被 dpkg 剥成非法 UTF-8：")
    for lineno, stripped in hazards:
        try:
            shown = stripped[-20:].decode("utf-8", "backslashreplace")
        except Exception:  # pragma: no cover - decode with backslashreplace 不会抛
            shown = repr(stripped[-20:])
        print(f"  第 {lineno} 行：...{shown}    {describe(stripped)}")
        print(f"          末尾字节：{stripped[-4:].hex(' ').upper()}")
    print("修法：在该行行尾补一个非空白字符（补空格无效），或改写结尾词。")
    return 1


def read_tag_note(tag: str) -> bytes:
    result = subprocess.run(
        ["git", "tag", "-l", "--format=%(contents)", tag],
        capture_output=True,
        check=True,
    )
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="发布说明行尾预检（dpkg/Perl \\s 坏字节）")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tag-note", metavar="FILE", help="准备写进 tag 注释的文本文件")
    source.add_argument("--tag", metavar="TAG", help="已存在 tag 的注释（git tag -a）")
    source.add_argument("--changes", metavar="FILE", help="打包产物 .changes，做上传前复核")
    source.add_argument("--stdin", action="store_true", help="从标准输入读文本")
    args = parser.parse_args()

    if args.tag_note:
        with open(args.tag_note, "rb") as handle:
            return report(args.tag_note, handle.read())
    if args.changes:
        with open(args.changes, "rb") as handle:
            return report(args.changes, handle.read())
    if args.tag:
        return report(f"tag {args.tag}", read_tag_note(args.tag))
    return report("<stdin>", sys.stdin.buffer.read())


if __name__ == "__main__":
    raise SystemExit(main())
