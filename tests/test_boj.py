"""백준 수집본 적재 테스트.

실제 1.4GB 수집본에 의존하지 않는다. 같은 스키마의 작은 DB를 만들어 쓴다.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tcgen import boj
from tcgen.config import BOJ_DB_PATH
from tcgen.models import FIGURE_ROLE_SPEC

SCHEMA = """
CREATE TABLE problems (
    problem_id INTEGER PRIMARY KEY,
    title TEXT,
    time_limit TEXT,
    memory_limit TEXT,
    submissions INTEGER,
    accepted INTEGER,
    solved_count INTEGER,
    accept_rate TEXT,
    source_url TEXT NOT NULL,
    content_text TEXT,
    parsed_json TEXT NOT NULL,
    lang_payload_json TEXT,
    raw_html TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE problem_sections (
    problem_id INTEGER NOT NULL,
    section_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    heading TEXT,
    body_html TEXT NOT NULL,
    body_text TEXT NOT NULL,
    PRIMARY KEY (problem_id, section_id, sequence)
);
CREATE TABLE problem_samples (
    problem_id INTEGER NOT NULL,
    sample_no INTEGER NOT NULL,
    input_text TEXT,
    output_text TEXT,
    input_html TEXT,
    output_html TEXT,
    PRIMARY KEY (problem_id, sample_no)
);
"""


def _insert_problem(
    connection: sqlite3.Connection,
    problem_id: int,
    *,
    title: str = "A+B",
    time_limit: str = "2 초",
    memory_limit: str = "128 MB",
    sections: tuple[tuple[str, int, str, str], ...] = (),
    samples: tuple[tuple[int, str, str], ...] = (),
) -> None:
    connection.execute(
        "INSERT INTO problems (problem_id, title, time_limit, memory_limit, source_url,"
        " parsed_json, raw_html, content_sha256, status_code, fetched_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            problem_id,
            title,
            time_limit,
            memory_limit,
            f"https://www.acmicpc.net/problem/{problem_id}",
            "{}",
            "<html></html>",
            "sha",
            200,
            "2026-04-15T12:54:21+00:00",
            "2026-04-15T12:54:21+00:00",
        ),
    )
    for section_id, sequence, heading, body_html in sections:
        connection.execute(
            "INSERT INTO problem_sections VALUES (?,?,?,?,?,?)",
            (problem_id, section_id, sequence, heading, body_html, ""),
        )
    for sample_no, input_text, output_text in samples:
        connection.execute(
            "INSERT INTO problem_samples VALUES (?,?,?,?,?,?)",
            (problem_id, sample_no, input_text, output_text, "", ""),
        )
    connection.commit()


DEFAULT_SECTIONS = (
    ("description", 1, "문제", '<div class="problem-text"><p>두 수를 더하시오.</p></div>'),
    ("input", 2, "입력", '<div class="problem-text"><p>A와 B가 주어진다.</p></div>'),
    ("output", 3, "출력", '<div class="problem-text"><p>A+B를 출력한다.</p></div>'),
    ("limit", 4, "제한", '<div class="problem-text"><p>0 &lt; A, B &lt; 10</p></div>'),
    ("hint", 5, "힌트", '<div class="problem-text"><p>쉬운 문제다.</p></div>'),
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "boj.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    _insert_problem(
        connection,
        1000,
        sections=DEFAULT_SECTIONS,
        samples=((1, "1 2\r\n", "3\r\n"), (2, "3 4\r\n", "7\r\n")),
    )
    connection.close()
    return path


@pytest.fixture
def images_dir(tmp_path: Path) -> Path:
    path = tmp_path / "images"
    path.mkdir()
    return path


def load(db_path: Path, images_dir: Path, problem_id: int = 1000) -> boj.BojImport:
    return boj.load_boj_problem(problem_id, db_path=db_path, images_dir=images_dir)


class TestLimitParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2 초", 2000),
            ("1 초", 1000),
            ("1.5 초", 1500),
            ("0.5 초", 500),
            ("0.15 초 (추가 시간 없음) ( 하단 참고 )", 150),
            ("1 초 (추가 시간 없음)", 1000),
        ],
    )
    def test_parses_seconds(self, text, expected):
        assert boj.parse_time_limit(text) == expected

    @pytest.mark.parametrize("text", ["서브태스크 참고", "서브태스크 참고 (추가 시간 없음)", "", None])
    def test_unparsable_time_is_none(self, text):
        assert boj.parse_time_limit(text) is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("128 MB", 128), ("1024 MB (추가 메모리 없음)", 1024), ("512 MB", 512)],
    )
    def test_parses_megabytes(self, text, expected):
        assert boj.parse_memory_limit(text) == expected

    def test_unparsable_memory_is_none(self):
        assert boj.parse_memory_limit("제한 없음") is None


class TestHtmlToText:
    def test_superscript_becomes_caret(self):
        """``2<sup>31</sup>``이 ``2 31``이 되면 제약이 조용히 바뀐다."""
        text = boj.html_to_text("<p>0 ≤ x &lt; 2<sup>31</sup></p>")
        assert "2^31" in text

    def test_subscript_becomes_underscore(self):
        assert "a_1" in boj.html_to_text("<p>a<sub>1</sub></p>")

    def test_table_keeps_rows_and_columns(self):
        html = "<table><tr><td>1</td><td>0</td></tr><tr><td>0</td><td>1</td></tr></table>"
        assert boj.html_to_text(html).splitlines() == ["1 | 0", "0 | 1"]

    def test_table_ignores_whitespace_between_tags(self):
        """실제 지문은 태그 사이에 줄바꿈이 들어 있다. 그것이 셀이 되면 안 된다."""
        html = """
        <table>
        <tbody>
        <tr>
        <td style="text-align:center">1</td>
        <td style="text-align:center">0</td>
        </tr>
        </tbody>
        </table>
        """
        assert boj.html_to_text(html).splitlines() == ["1 | 0"]

    def test_cells_without_a_row_still_form_one_row(self):
        """서브태스크 표의 머리글은 ``<tr>`` 없이 ``<th>``만 있다(2545번)."""
        html = (
            "<table><thead><th>번호</th><th>배점</th><th>제한</th></thead>"
            "<tbody><tr><td>Easy</td><td>1</td><td>N ≤ 1000</td></tr></tbody></table>"
        )
        assert boj.html_to_text(html).splitlines() == [
            "번호 | 배점 | 제한",
            "Easy | 1 | N ≤ 1000",
        ]

    def test_table_header_cells_are_columns_too(self):
        html = "<table><tr><th>N</th><th>점수</th></tr><tr><td>10</td><td>20</td></tr></table>"
        assert boj.html_to_text(html).splitlines() == ["N | 점수", "10 | 20"]

    def test_unclosed_row_is_not_swallowed(self):
        """실제 지문에 ``</tr>`` 없이 다음 ``<tr>``이 오는 표가 있다(17432번)."""
        html = "<table><tr><td>첫 행</td><tr><td>둘째 행</td></tr></tr></table>"
        assert boj.html_to_text(html).splitlines() == ["첫 행", "둘째 행"]

    def test_block_content_inside_a_cell_survives(self):
        """셀 안의 ``<pre>`` 의사코드가 사라지면 문제가 다른 문제가 된다."""
        html = "<table><tr><td><pre>for i in range(n):\n  x += 1</pre></td></tr></table>"
        text = boj.html_to_text(html)
        assert text.splitlines() == ["for i in range(n):", "  x += 1"]

    def test_multiple_blocks_in_one_cell_stay_on_one_row(self):
        """한 셀에 문단이 여럿이면 행이 쪼개져 옆 열의 값과 뒤섞인다(2453번)."""
        html = (
            "<table><tr>"
            "<td><p>가</p><p>나</p></td>"
            "<td><p>다</p><p>라</p></td>"
            "</tr></table>"
        )
        assert boj.html_to_text(html).splitlines() == ["가 나 | 다 라"]

    def test_nested_table_does_not_break_the_outer_row(self):
        """셀 안의 표가 바깥 행을 해체하면 라벨과 표의 대응이 뒤바뀐다(3340번)."""
        html = (
            "<table><tr>"
            "<td>A1</td>"
            "<td><table><tr><td>in1</td><td>in2</td></tr></table></td>"
            "<td>A3</td>"
            "</tr>"
            "<tr><td>B1</td><td>B2</td><td>B3</td></tr></table>"
        )
        lines = boj.html_to_text(html).splitlines()
        assert lines[-1] == "B1 | B2 | B3"
        assert len(lines) == 2, f"바깥 표는 2행이어야 한다: {lines}"
        assert "in1 | in2" in lines[0]
        assert lines[0].startswith("A1 |") and lines[0].endswith("| A3")

    def test_style_and_script_are_not_statement_text(self):
        """CSS가 지문 첫 줄로 들어간다(18823번). 날짜 리터럴이 제약처럼 보인다(15956번)."""
        html = (
            "<style>.cell { width: 40px; }</style>"
            "<p>실제 지문</p>"
            '<script>var endTime = new Date("9999-08-04");</script>'
        )
        assert boj.html_to_text(html) == "실제 지문"

    def test_empty_cells_keep_their_position(self):
        """빈 칸을 지우면 열이 밀려 표가 다른 표가 된다."""
        html = "<table><tr><td>1</td><td></td><td>3</td></tr></table>"
        assert boj.html_to_text(html).splitlines() == ["1 |  | 3"]

    def test_paragraphs_are_separated(self):
        text = boj.html_to_text("<p>첫째 문단</p><p>둘째 문단</p>")
        assert text == "첫째 문단\n\n둘째 문단"

    def test_entities_are_unescaped(self):
        assert boj.html_to_text("<p>A &lt;= B &amp;&amp; C</p>") == "A <= B && C"

    def test_list_items_are_separate_lines(self):
        text = boj.html_to_text("<ul><li>하나</li><li>둘</li></ul>")
        assert text.splitlines() == ["하나", "둘"]

    def test_images_become_numbered_refs(self):
        counter = boj.FigureCounter()
        first = boj.html_to_text('<p>보기 <img src="a.png"></p>', figures=counter)
        second = boj.html_to_text('<p>또 <img src="b.png"></p>', figures=counter)
        assert "[그림 1]" in first
        assert "[그림 2]" in second
        assert counter.sources == ["a.png", "b.png"]

    def test_pre_keeps_line_breaks(self):
        assert boj.html_to_text("<pre>1 2\n3 4</pre>").splitlines() == ["1 2", "3 4"]

    def test_pre_keeps_indentation(self):
        """``<pre>`` 안에서는 들여쓰기가 내용이다. 격자와 아스키 그림이 여기 있다."""
        html = "<pre>    +---+\n    |   |\n    +---+</pre>"
        assert boj.html_to_text(html).splitlines() == ["    +---+", "    |   |", "    +---+"]

    def test_text_outside_pre_is_still_trimmed(self):
        assert boj.html_to_text("<p>   띄어 쓴 문단   </p>") == "띄어 쓴 문단"

    def test_unclosed_pre_still_produces_text(self):
        assert "1 2" in boj.html_to_text("<p>예</p><pre>1 2")


class TestLoadProblem:
    def test_maps_sections_to_fields(self, db_path, images_dir):
        problem = load(db_path, images_dir).problem
        assert problem.title == "A+B"
        assert problem.statement == "두 수를 더하시오."
        assert problem.input_spec == "A와 B가 주어진다."
        assert problem.output_spec == "A+B를 출력한다."
        assert "0 < A, B < 10" in problem.constraints
        assert "쉬운 문제다." in problem.hints

    def test_slug_identifies_the_source(self, db_path, images_dir):
        assert load(db_path, images_dir).problem.slug == "boj-1000"

    def test_examples_are_normalized_to_lf(self, db_path, images_dir):
        examples = load(db_path, images_dir).problem.examples
        assert len(examples) == 2
        assert examples[0].input == "1 2\n"
        assert examples[0].output == "3\n"
        assert examples[1].input == "3 4\n"

    def test_limits_come_from_the_record(self, db_path, images_dir):
        limits = load(db_path, images_dir).problem.limits
        assert limits.time_ms == 2000
        assert limits.memory_mb == 128
        assert limits.assumed is False

    def test_unparsable_limit_falls_back_and_is_marked_assumed(self, tmp_path, images_dir):
        path = tmp_path / "sub.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        _insert_problem(
            connection, 1001, time_limit="서브태스크 참고", sections=DEFAULT_SECTIONS
        )
        connection.close()
        result = boj.load_boj_problem(1001, db_path=path, images_dir=images_dir)
        assert result.problem.limits.assumed is True
        assert any("시간" in note for note in result.notes)

    def test_subtask_sections_go_to_constraints(self, tmp_path, images_dir):
        path = tmp_path / "st.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        _insert_problem(
            connection,
            1002,
            sections=DEFAULT_SECTIONS
            + (("subtask1", 6, "서브태스크 1 (30점)", "<p>N ≤ 100</p>"),),
        )
        connection.close()
        problem = boj.load_boj_problem(1002, db_path=path, images_dir=images_dir).problem
        assert "서브태스크 1 (30점)" in problem.constraints
        assert "N ≤ 100" in problem.constraints

    def test_unknown_sections_are_kept_in_hints(self, tmp_path, images_dir):
        path = tmp_path / "custom.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        _insert_problem(
            connection,
            1003,
            sections=DEFAULT_SECTIONS
            + (("custom_scoring", 6, "점수", "<p>부분 점수가 있다.</p>"),),
        )
        connection.close()
        problem = boj.load_boj_problem(1003, db_path=path, images_dir=images_dir).problem
        assert "점수" in problem.hints
        assert "부분 점수가 있다." in problem.hints

    @pytest.mark.parametrize(
        ("missing", "label"),
        [("description", "문제 설명"), ("input", "입력 설명"), ("output", "출력 설명")],
    )
    def test_each_empty_field_is_reported(self, tmp_path, images_dir, missing, label):
        """출력 설명 하나만 비어도 알려야 한다. 모르면 채워진 줄 안다."""
        path = tmp_path / f"no_{missing}.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        _insert_problem(
            connection,
            1007,
            sections=tuple(item for item in DEFAULT_SECTIONS if item[0] != missing),
        )
        connection.close()
        result = boj.load_boj_problem(1007, db_path=path, images_dir=images_dir)
        assert any(label in note for note in result.notes), result.notes

    def test_missing_problem_raises(self, db_path, images_dir):
        with pytest.raises(boj.BojError, match="9999"):
            boj.load_boj_problem(9999, db_path=db_path, images_dir=images_dir)

    def test_missing_database_raises(self, tmp_path, images_dir):
        with pytest.raises(boj.BojError, match="수집본"):
            boj.load_boj_problem(1000, db_path=tmp_path / "없음.db", images_dir=images_dir)

    @pytest.mark.parametrize("problem_id", [0, 999, -1, 10**9])
    def test_out_of_range_id_raises(self, db_path, images_dir, problem_id):
        with pytest.raises(boj.BojError, match="문제 번호"):
            boj.load_boj_problem(problem_id, db_path=db_path, images_dir=images_dir)

    def test_database_is_opened_read_only(self, db_path, images_dir):
        """수집본은 이 도구가 만든 것이 아니다. 어떤 경로로도 쓰지 않는다."""
        with boj.connect(db_path) as connection, pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM problems")


class TestFigures:
    @pytest.fixture
    def picture_db(self, tmp_path: Path) -> Path:
        path = tmp_path / "pic.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        _insert_problem(
            connection,
            1004,
            sections=(
                (
                    "description",
                    1,
                    "문제",
                    (
                        '<p>그림을 보라.</p>'
                        '<p><img src="https://upload.acmicpc.net/a"></p>'
                        '<p><img src="https://upload.acmicpc.net/b"></p>'
                    ),
                ),
            ),
        )
        connection.close()
        return path

    def test_images_become_figures_with_local_files(self, picture_db, images_dir):
        folder = images_dir / "1004"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"first")
        (folder / "2.avif").write_bytes(b"second")

        result = boj.load_boj_problem(1004, db_path=picture_db, images_dir=images_dir)
        assert [figure.ref for figure in result.problem.figures] == ["[그림 1]", "[그림 2]"]
        assert [path.name for path in result.images] == ["1.png", "2.avif"]

    def test_figure_markers_stay_in_the_statement(self, picture_db, images_dir):
        folder = images_dir / "1004"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"first")
        (folder / "2.avif").write_bytes(b"second")

        statement = boj.load_boj_problem(
            1004, db_path=picture_db, images_dir=images_dir
        ).problem.statement
        assert "[그림 1]" in statement
        assert "[그림 2]" in statement

    def test_figures_start_critical_and_untranscribed(self, picture_db, images_dir):
        """전사 없는 그림을 decorative로 두면 풀이가 조용히 틀린다."""
        folder = images_dir / "1004"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"first")
        (folder / "2.avif").write_bytes(b"second")

        problem = boj.load_boj_problem(
            1004, db_path=picture_db, images_dir=images_dir
        ).problem
        assert all(figure.role == FIGURE_ROLE_SPEC for figure in problem.figures)
        assert len(problem.unresolved_figures) == 2

    def test_duplicate_download_names_are_ignored(self, picture_db, images_dir):
        """``1(1).png``은 중복 내려받기다. 순번 대응을 어긋나게 두면 안 된다."""
        folder = images_dir / "1004"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"first")
        (folder / "1(1).png").write_bytes(b"duplicate")
        (folder / "2.avif").write_bytes(b"second")

        result = boj.load_boj_problem(1004, db_path=picture_db, images_dir=images_dir)
        assert [path.name for path in result.images] == ["1.png", "2.avif"]

    def test_gap_in_numbering_is_refused(self, picture_db, images_dir):
        """``1.png``+``3.png``에 그림 2개면 개수는 맞는다. 짝은 틀린다."""
        folder = images_dir / "1004"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"first")
        (folder / "3.png").write_bytes(b"third")

        result = boj.load_boj_problem(1004, db_path=picture_db, images_dir=images_dir)
        assert result.images == ()
        assert any("3.png" in note for note in result.notes)

    def test_duplicate_numbering_is_refused(self, picture_db, images_dir):
        """``1.avif``+``1.webp``는 어느 쪽이 1번인지 파일명 우연에 달린다."""
        folder = images_dir / "1004"
        folder.mkdir()
        (folder / "1.avif").write_bytes(b"one")
        (folder / "1.webp").write_bytes(b"also one")

        assert boj.load_boj_problem(1004, db_path=picture_db, images_dir=images_dir).images == ()

    def test_count_mismatch_reports_instead_of_guessing(self, picture_db, images_dir):
        """개수가 어긋나면 위치 대응이 틀어진다. 틀린 짝을 지어 주느니 비운다."""
        folder = images_dir / "1004"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"only one")

        result = boj.load_boj_problem(1004, db_path=picture_db, images_dir=images_dir)
        assert result.images == ()
        assert len(result.problem.figures) == 2
        assert any("그림" in note for note in result.notes)

    def test_no_images_means_no_figures(self, db_path, images_dir):
        result = load(db_path, images_dir)
        assert result.problem.figures == ()
        assert result.images == ()


class TestUntrustedCollection:
    """수집본은 남에게 받아 푸는 1.9GB 압축 파일이다. 내용을 믿지 않는다."""

    def test_connection_is_closed_after_loading(self, db_path, images_dir):
        """1.4GB DB 핸들이 쌓이면 사용자가 수집본을 바꿀 수 없다."""
        opened: list[sqlite3.Connection] = []
        real_connect = boj.connect

        def spy(path=None):
            connection = real_connect(path)
            opened.append(connection)
            return connection

        boj.connect = spy
        try:
            load(db_path, images_dir)
        finally:
            boj.connect = real_connect
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_corrupt_collection_is_reported_not_crashed(self, tmp_path, images_dir):
        path = tmp_path / "broken.db"
        path.write_bytes(b"this is not a database")
        with pytest.raises(boj.BojUnavailable):
            boj.load_boj_problem(1000, db_path=path, images_dir=images_dir)

    def test_schema_mismatch_is_reported_not_crashed(self, tmp_path, images_dir):
        path = tmp_path / "other.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE unrelated (x INTEGER)")
        connection.close()
        with pytest.raises(boj.BojUnavailable):
            boj.load_boj_problem(1000, db_path=path, images_dir=images_dir)

    def test_oversized_section_is_skipped_with_a_note(self, tmp_path, images_dir):
        path = tmp_path / "huge.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        huge = "<p>" + "가" * (boj.MAX_SECTION_HTML_BYTES + 1) + "</p>"
        _insert_problem(
            connection,
            1005,
            sections=DEFAULT_SECTIONS + (("custom_huge", 6, "덩어리", huge),),
        )
        connection.close()
        result = boj.load_boj_problem(1005, db_path=path, images_dir=images_dir)
        assert "가가가" not in result.problem.hints
        assert any("건너뛰었습니다" in note for note in result.notes)

    def test_overlong_field_is_capped_with_a_note(self, tmp_path, images_dir):
        """폼 입력에만 상한이 있으면 상한이 없는 것과 같다."""
        path = tmp_path / "long.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        limit = boj.FIELD_LIMITS["hints"]
        chunks = tuple(
            (f"custom_{index}", 6 + index, f"조각{index}", "<p>" + "나" * 20_000 + "</p>")
            for index in range(limit // 20_000 + 2)
        )
        _insert_problem(connection, 1006, sections=DEFAULT_SECTIONS + chunks)
        connection.close()
        result = boj.load_boj_problem(1006, db_path=path, images_dir=images_dir)
        assert len(result.problem.hints) == limit
        assert any("잘렸습니다" in note for note in result.notes)

    def test_unknown_extensions_are_not_copied(self, tmp_path, images_dir):
        """``1.exe``가 그림 자리에 있으면 그림이 아니다."""
        folder = images_dir / "1000"
        folder.mkdir()
        (folder / "1.exe").write_bytes(b"not an image")
        assert boj._numbered_images(folder, images_dir) == ()

    def test_oversized_image_is_not_copied(self, images_dir, monkeypatch):
        folder = images_dir / "1000"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"x" * 64)
        monkeypatch.setattr(boj, "MAX_FIGURE_BYTES", 32)
        assert boj._numbered_images(folder, images_dir) == ()

    def test_folder_pointing_outside_is_refused(self, tmp_path, images_dir, monkeypatch):
        """Windows junction은 ``is_symlink()``가 False다. 실체 경로로 막아야 한다."""
        outside = tmp_path / "secrets"
        outside.mkdir()
        (outside / "1.png").write_bytes(b"secret")
        folder = images_dir / "1000"

        # junction은 CI에서 만들 수 없으므로, 그것이 만들어 내는 상태 —
        # '폴더의 실체 경로가 images 밖' — 를 그대로 흉내 낸다.
        monkeypatch.setattr(
            Path, "resolve", lambda self, strict=False: outside if self == folder else self
        )
        assert boj._numbered_images(folder, images_dir) == ()

    def test_linked_image_is_refused(self, tmp_path, images_dir):
        """``1.png``이라는 이름의 링크 하나로 집 안의 아무 파일이나 복사된다."""
        import os

        secret = tmp_path / "secret.txt"
        secret.write_text("비밀", encoding="utf-8")
        folder = images_dir / "1000"
        folder.mkdir()
        try:
            os.symlink(secret, folder / "1.png")
        except (OSError, NotImplementedError):
            pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없다")
        assert boj._numbered_images(folder, images_dir) == ()


class TestImportIntoWorkspace:
    def test_creates_workspace_named_after_the_problem(self, db_path, images_dir, tmp_path):
        workspace, _ = boj.import_into_workspace(
            1000, db_path=db_path, images_dir=images_dir, base_dir=tmp_path / "ws"
        )
        assert workspace.slug == "boj-1000"
        assert workspace.load_problem().title == "A+B"

    def test_copies_figures_into_the_workspace(self, tmp_path, images_dir):
        path = tmp_path / "pic.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        _insert_problem(
            connection,
            1004,
            sections=(("description", 1, "문제", '<p><img src="https://upload.acmicpc.net/a"></p>'),),
        )
        connection.close()
        folder = images_dir / "1004"
        folder.mkdir()
        (folder / "1.PNG").write_bytes(b"first")

        workspace, notes = boj.import_into_workspace(
            1004, db_path=path, images_dir=images_dir, base_dir=tmp_path / "ws"
        )
        copied = workspace.figures_dir / "figure-1.png"
        assert copied.read_bytes() == b"first"
        assert not [note for note in notes if "복사 실패" in note]

    def test_source_images_are_left_untouched(self, tmp_path, images_dir):
        """수집본은 다시 만들려면 백준에 7일치 요청을 다시 보내야 한다."""
        path = tmp_path / "pic.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        _insert_problem(
            connection,
            1004,
            sections=(("description", 1, "문제", '<p><img src="https://upload.acmicpc.net/a"></p>'),),
        )
        connection.close()
        folder = images_dir / "1004"
        folder.mkdir()
        original = folder / "1.png"
        original.write_bytes(b"first")

        boj.import_into_workspace(
            1004, db_path=path, images_dir=images_dir, base_dir=tmp_path / "ws"
        )
        assert original.read_bytes() == b"first"

    def test_notes_survive_to_the_caller(self, tmp_path, images_dir):
        path = tmp_path / "sub.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        _insert_problem(
            connection, 1001, time_limit="서브태스크 참고", sections=DEFAULT_SECTIONS
        )
        connection.close()
        _, notes = boj.import_into_workspace(
            1001, db_path=path, images_dir=images_dir, base_dir=tmp_path / "ws"
        )
        assert any("시간 제한" in note for note in notes)


@pytest.mark.integration
@pytest.mark.skipif(not BOJ_DB_PATH.is_file(), reason="실제 수집본이 없는 환경")
class TestRealCollection:
    """실제 수집본에 대한 연기 테스트.

    단위 테스트가 모두 통과하는 상태로도 변환기가 실제 지문에서 깨질 수 있다.
    여기서 걸리는 것들이 지금까지 둘 있었다 — ``</tr>`` 누락으로 사라진 행과
    ``<pre>`` 들여쓰기 유실이다.
    """

    def test_sampled_sections_convert_without_error(self):
        import random

        with closing(boj.connect()) as connection:
            rows = connection.execute(
                "SELECT problem_id, section_id, body_html FROM problem_sections"
                " WHERE body_html LIKE '%<table%' OR body_html LIKE '%<pre%'"
                " LIMIT 4000"
            ).fetchall()
        assert rows, "표·<pre>를 담은 섹션이 하나도 없다면 수집본이 이상한 것이다"
        for problem_id, section_id, body_html in random.Random(7).sample(rows, min(400, len(rows))):
            try:
                boj.html_to_text(body_html, figures=boj.FigureCounter())
            except Exception as error:  # noqa: BLE001 - 어느 섹션인지 알려야 한다
                pytest.fail(f"{problem_id}번 '{section_id}' 섹션 변환 실패: {error!r}")

    def test_every_table_row_stays_on_one_line(self):
        """행 하나가 여러 줄로 쪼개지면 옆 열의 값과 뒤섞인다.

        본문에는 BNF 문법처럼 ``|``를 쓰는 지문이 있으므로(20241번) 섹션
        전체가 아니라 **표 조각만** 떼어 센다.
        """
        import re

        table_block = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
        with closing(boj.connect()) as connection:
            rows = connection.execute(
                "SELECT problem_id, section_id, body_html FROM problem_sections"
                " WHERE body_html LIKE '%<td%' AND body_html NOT LIKE '%<pre%' LIMIT 1500"
            ).fetchall()
        checked = 0
        broken = []
        for problem_id, section_id, body_html in rows:
            for fragment in table_block.findall(body_html):
                # 중첩 표는 안쪽이 바깥 셀로 접히므로 행 수를 이렇게 셀 수 없다.
                if fragment.lower().count("<table") != 1:
                    continue
                checked += 1
                # ``<tr>`` 없는 머리글이 한 행을 더 만들 수 있고(2545번),
                # ``rowspan``이 걸린 빈 행은 한 줄도 만들지 않는다(4691번).
                # 결함은 **행보다 줄이 많아지는** 쪽이다 — 그때 한 행이 쪼개져
                # 옆 열의 값과 뒤섞인다.
                lower = fragment.lower()
                allowed = (
                    lower.count("<tr")
                    + (1 if "<th" in lower.split("<tr")[0] else 0)
                    + lower.count("<caption")  # 표 제목도 한 줄을 차지한다(6901번).
                )
                produced = len(boj.html_to_text(fragment).splitlines())
                if produced > allowed:
                    broken.append((problem_id, section_id, allowed, produced))
        assert checked > 100, f"검사한 표가 너무 적다: {checked}"
        assert not broken, f"행이 쪼개진 표: {broken[:5]}"

    def test_known_problem_loads_with_its_real_limits(self):
        problem = boj.load_boj_problem(1000).problem
        assert problem.title == "A+B"
        assert problem.limits.to_dict() == {"time_ms": 2000, "memory_mb": 128, "assumed": False}
        assert problem.examples[0].input == "1 2\n"
