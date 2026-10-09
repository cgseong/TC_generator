"""비교기 규칙 고정 테스트 (PRD AC-8).

이 테스트가 깨지면 '로컬 통과 = 서버 통과' 보장이 무너진다.
"""

import hashlib

from tcgen import compare


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class TestCanonicalOutput:
    def test_crlf_becomes_lf(self):
        assert compare.canonical_output(b"1 2\r\n3\r\n") == b"1 2\n3"

    def test_trailing_newlines_removed(self):
        assert compare.canonical_output(b"5\n\n\n") == b"5"

    def test_trailing_spaces_and_tabs_removed(self):
        assert compare.canonical_output(b"5  \t\n") == b"5"

    def test_interior_trailing_space_is_preserved(self):
        # rstrip()은 파일 끝만 자른다. 줄 중간의 잔여 공백은 남아 WA가 된다.
        assert compare.canonical_output(b"a \nb\n") == b"a \nb"

    def test_leading_whitespace_is_preserved(self):
        assert compare.canonical_output(b"  a\n") == b"  a"

    def test_empty_stays_empty(self):
        assert compare.canonical_output(b"") == b""

    def test_whitespace_only_becomes_empty(self):
        assert compare.canonical_output(b"\r\n  \n") == b""


class TestExpectedMd5:
    def test_matches_server_upload_rule(self):
        # 서버: md5(zip에서 읽은 내용.replace(CRLF, LF).rstrip())
        raw = b"3\r\n1 2 3\r\n"
        assert compare.expected_md5(raw) == md5(b"3\n1 2 3")

    def test_judge_rule_has_no_crlf_normalization(self):
        # 채점기: md5(제출 출력.rstrip()) — CRLF 정규화가 없다.
        raw = b"3\r\n"
        assert compare.judge_md5(raw) == md5(b"3")
        assert compare.judge_md5(b"a\r\nb\n") == md5(b"a\r\nb")

    def test_lf_only_content_agrees_on_both_sides(self):
        # .out을 LF로만 기록해야 두 경로의 해시가 일치한다 (F3-6).
        lf = b"1\n2\n3\n"
        assert compare.expected_md5(lf) == compare.judge_md5(lf)

    def test_crlf_content_disagrees_across_sides(self):
        crlf = b"1\r\n2\r\n"
        assert compare.expected_md5(crlf) != compare.judge_md5(crlf)


class TestOutputsMatch:
    def test_trailing_newline_difference_passes(self):
        assert compare.outputs_match(b"5\n", b"5")

    def test_windows_crlf_output_passes(self):
        # Windows에서 파이썬이 텍스트 모드로 찍으면 CRLF가 되어 돌아온다.
        assert compare.outputs_match(b"5\n", b"5\r\n")

    def test_interior_trailing_space_fails(self):
        assert not compare.outputs_match(b"a\nb\n", b"a \nb\n")

    def test_different_values_fail(self):
        assert not compare.outputs_match(b"5\n", b"6\n")

    def test_both_empty_pass(self):
        assert compare.outputs_match(b"", b"\n\n")


class TestDiagnostics:
    def test_has_crlf(self):
        assert compare.has_crlf(b"a\r\nb")
        assert not compare.has_crlf(b"a\nb")

    def test_lines_with_trailing_space_reports_1_based_line_numbers(self):
        raw = b"ok\nbad \nalso bad\t\nok\n"
        assert compare.lines_with_trailing_space(raw) == (2, 3)

    def test_last_line_trailing_space_is_not_reported(self):
        # 파일 끝 공백은 rstrip으로 무시되므로 문제가 아니다.
        assert compare.lines_with_trailing_space(b"a\nb  \n") == ()

    def test_clean_output_has_no_findings(self):
        assert compare.lines_with_trailing_space(b"1 2 3\n4 5 6\n") == ()
