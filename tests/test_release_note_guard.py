"""发布说明行尾预检的回归测试。

样例直接取自 tag bianbu/0.1.24 的真实事故：发布说明有一行以「加」
（E5 8A A0）结尾，dpkg 把末字节 0xA0 当空白剥掉，upload 阶段读
.changes 时报 UnicodeDecodeError。
"""
import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "release_note_guard",
    Path(__file__).parents[1] / "scripts" / "release_note_guard.py",
)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def changelog(*body_lines: str) -> bytes:
    text = (
        "bianbu-agent-memory (0.0.1) unstable; urgency=medium\n\n"
        + "".join(f"  {line}\n" for line in body_lines)
        + "\n -- qinyanli <yanli.qin@spacemit.com>  Thu, 01 Jan 2026 00:00:00 +0800\n"
    )
    return text.encode("utf-8")


class StripPerlBlanksTests(unittest.TestCase):
    def test_strips_ascii_whitespace(self):
        self.assertEqual(guard.strip_perl_blanks(b"text \t\r"), b"text")

    def test_strips_latin1_nbsp_and_nel(self):
        # 0xA0/0x85 是 Perl \s 在字节视角下的成员
        self.assertEqual(guard.strip_perl_blanks(b"text\xa0"), b"text")
        self.assertEqual(guard.strip_perl_blanks(b"text\x85"), b"text")

    def test_keeps_trailing_non_blank_bytes(self):
        # 「中」(E4 B8 AD) 末字节不是 Perl 空白，原样保留
        self.assertEqual(
            guard.strip_perl_blanks("text中".encode("utf-8")), "text中".encode("utf-8")
        )

    def test_strips_a0_char_at_end_leaving_truncated_prefix(self):
        # 「加」(E5 8A A0) 行尾：0xA0 被剥掉，只剩 E5 8A
        self.assertEqual(
            guard.strip_perl_blanks("text加".encode("utf-8")), b"text\xe5\x8a"
        )


class ScanTests(unittest.TestCase):
    def test_flags_line_ending_in_a0_char(self):
        hazards = guard.scan(changelog("* 修复：MemoryRecallItem 增加"))
        self.assertEqual(len(hazards), 1)
        lineno, stripped = hazards[0]
        self.assertEqual(lineno, 3)
        self.assertEqual(stripped, "  * 修复：MemoryRecallItem 增".encode("utf-8") + b"\xe5\x8a")
        self.assertEqual(guard.eaten_bytes(stripped), 1)

    def test_accepts_midline_a0_char(self):
        # 「加」不在行尾时是安全的
        self.assertEqual(guard.scan(changelog("* 增加 tags 回显")), [])

    def test_accepts_line_ending_in_safe_char(self):
        self.assertEqual(guard.scan(changelog("* 修复：MemoryRecallItem 更新")), [])

    def test_trailing_space_does_not_rescue(self):
        # 补空格没用：空格本身也会被剥掉，字符仍在行尾
        self.assertEqual(len(guard.scan(changelog("* MemoryRecallItem 增加 "))), 1)

    def test_trailing_punctuation_rescues(self):
        # 补一个非空白字符即可保住末字节
        self.assertEqual(guard.scan(changelog("* MemoryRecallItem 增加。")), [])
        self.assertEqual(guard.scan(changelog("* MemoryRecallItem 增加(见下)")), [])

    def test_ignores_blank_lines(self):
        self.assertEqual(guard.scan(b"\n   \n\t\n"), [])

    def test_reports_every_hazard_line(self):
        hazards = guard.scan(changelog("* 第一处 增加", "* 安全行", "* 第二处 增加"))
        self.assertEqual([lineno for lineno, _ in hazards], [3, 5])


class ReportTests(unittest.TestCase):
    def test_report_returns_zero_and_prints_ok_when_clean(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = guard.report("clean", changelog("* 修复：MemoryRecallItem 更新"))
        self.assertEqual(code, 0)
        self.assertIn("OK", buffer.getvalue())

    def test_report_returns_one_and_names_line_when_dirty(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = guard.report("dirty", changelog("* 修复：MemoryRecallItem 增加"))
        self.assertEqual(code, 1)
        output = buffer.getvalue()
        self.assertIn("FAIL", output)
        self.assertIn("第 3 行", output)


if __name__ == "__main__":
    unittest.main()
