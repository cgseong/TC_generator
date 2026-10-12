"""백준 온라인 저지 수집본에서 문제를 읽어 온다 (``boj_problems/``).

문제 번호 하나로 지문·입출력 설명·제약·예제·시간/메모리 제한을 모두 채운다.
수집본이 이미 섹션 단위로 분해되어 있으므로 이 경로에는 LLM 파싱이 없다.
지문을 옮겨 적다 생기는 오차와 호출 비용이 통째로 사라진다.

두 가지를 특별히 지킨다.

- **읽기 전용.** 수집본은 이 도구가 만든 것이 아니고 다시 만들려면 백준에
  7일치 요청을 다시 보내야 한다. 연결 자체를 ``mode=ro``로 연다.
- **저장된 ``body_text``를 쓰지 않는다.** 거기서는 ``2<sup>31</sup>``이
  ``2 31``로, 표의 행과 열이 한 줄로 뭉개져 있다. 제약 조건이 조용히 바뀌는
  것이라 :func:`html_to_text`로 ``body_html``을 직접 옮긴다.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, Iterable, Sequence
from urllib.parse import quote

from tcgen.config import (
    BOJ_DB_PATH,
    BOJ_IMAGES_DIR,
    BOJ_MAX_PROBLEM_ID,
    BOJ_MIN_PROBLEM_ID,
    DEFAULT_MEMORY_LIMIT_MB,
    DEFAULT_TIME_LIMIT_MS,
)
from tcgen.models import FIGURE_ROLE_SPEC, Example, Figure, Limits, Problem
from tcgen.workspace import Workspace, create_workspace

SLUG_PREFIX: Final[str] = "boj-"
SOURCE_URL: Final[str] = "https://www.acmicpc.net/problem/{problem_id}"

#: 섹션을 문제 필드로 보내는 규칙. 어디에도 걸리지 않는 섹션은 힌트로 간다.
SECTION_STATEMENT: Final[str] = "description"
SECTION_INPUT: Final[str] = "input"
SECTION_OUTPUT: Final[str] = "output"
SECTION_LIMIT: Final[str] = "limit"
_CONSTRAINT_PREFIX: Final[str] = "subtask"

_SECONDS = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*초")
_MEGABYTES = re.compile(r"([0-9]+)\s*MB", re.IGNORECASE)

#: ``1.png``처럼 순번으로만 된 이름. ``1(1).png``는 중복 내려받기라 센다면
#: 그림과 파일의 위치 대응이 통째로 한 칸씩 밀린다. 확장자는 수집본에 실제로
#: 있는 것만 받는다. 이름이 아무 영숫자나 통과하면 ``1.exe``도 복사된다.
_IMAGE_NAME = re.compile(r"\A([0-9]+)\.(?:png|jpe?g|gif|bmp|webp|avif|svg)\Z", re.IGNORECASE)

#: 그림 한 장과 한 문제의 상한. 지문 그림은 수백 KB면 충분하다.
MAX_FIGURE_BYTES: Final[int] = 16 * 1024 * 1024
MAX_FIGURES: Final[int] = 50

#: 섹션 하나의 HTML 상한. 이보다 큰 것은 지문이 아니다.
MAX_SECTION_HTML_BYTES: Final[int] = 2 * 1024 * 1024

#: 문제 필드별 글자 수 상한. ``ProblemUpdate``(server.py)가 폼 입력에 거는
#: 것과 같은 값이다. 한쪽에만 상한이 있으면 상한이 없는 것과 같다.
FIELD_LIMITS: Final[dict[str, int]] = {
    "statement": 200_000,
    "input_spec": 50_000,
    "output_spec": 50_000,
    "constraints": 50_000,
    "hints": 50_000,
}

#: 앞뒤로 빈 줄을 두는 태그. 문단이 붙으면 어디서 끊기는지 알 수 없다.
_PARAGRAPH_TAGS: Final[frozenset[str]] = frozenset(
    {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
)
#: 줄만 바꾸는 태그. 목록 항목과 표의 행이 빈 줄로 벌어지면 표가 아니게 된다.
_LINE_TAGS: Final[frozenset[str]] = frozenset({"li", "tr"})
_BLOCK_TAGS: Final[frozenset[str]] = _PARAGRAPH_TAGS | _LINE_TAGS
#: 지문이 아닌 것들. ``HTMLParser``는 이 안의 내용도 ``handle_data``로 준다.
#: 걸러 내지 않으면 CSS가 지문 첫 줄이 되고(18823번), 스크립트의 날짜 리터럴이
#: 제약 조건처럼 보인다(15956번).
_MUTED_TAGS: Final[frozenset[str]] = frozenset({"script", "style"})
_CELL_SEPARATOR: Final[str] = " | "
#: ``<pre>`` 구간의 경계 표시. 들여쓰기를 정리 대상에서 빼기 위한 것이라
#: 지문에 나타날 수 없는 글자를 쓴다.
_PROTECTED: Final[str] = "\x00"


class BojError(Exception):
    """수집본에서 문제를 가져올 수 없는 상태. 요청이 잘못된 경우다."""


class BojNotFound(BojError):
    """번호는 멀쩡한데 그 문제가 수집본에 없다."""


class BojUnavailable(BojError):
    """수집본 자체가 없거나 열리지 않는다. 요청이 아니라 설치의 문제다."""


# --------------------------------------------------------------------- 제한


def parse_time_limit(text: str | None) -> int | None:
    """``"1.5 초 (추가 시간 없음)"`` → ``1500``. 읽을 수 없으면 ``None``.

    ``"서브태스크 참고"``처럼 숫자가 없는 값이 실제로 있다. 이때 기본값을
    조용히 쓰면 사람은 그 값이 지문에서 온 줄 안다.
    """
    match = _SECONDS.search(text or "")
    return round(float(match.group(1)) * 1000) if match else None


def parse_memory_limit(text: str | None) -> int | None:
    """``"1024 MB (추가 메모리 없음)"`` → ``1024``. 읽을 수 없으면 ``None``."""
    match = _MEGABYTES.search(text or "")
    return int(match.group(1)) if match else None


# ------------------------------------------------------------------ HTML


@dataclass
class FigureCounter:
    """그림 번호를 섹션들에 걸쳐 이어서 매긴다.

    지문·입력·출력에 흩어진 그림이 각자 1번부터 시작하면 ``[그림 1]``이
    가리키는 대상이 둘이 된다.
    """

    sources: list[str] = field(default_factory=list)

    def add(self, source: str) -> str:
        self.sources.append(source)
        return figure_ref(len(self.sources))


def figure_ref(number: int) -> str:
    """지문 본문과 :class:`~tcgen.models.Figure` 가 공유하는 그림 이름."""
    return f"[그림 {number}]"


@dataclass
class _Table:
    """표 하나를 만드는 동안의 상태. 중첩되므로 스택으로 쌓는다."""

    row: list[str] | None = None
    cell: list[str] | None = None


class _TextExtractor(HTMLParser):
    """지문 HTML을 사람이 읽는 텍스트로 옮긴다.

    표는 행과 열을 유지하고, 위·아래 첨자는 ``^``·``_``로 살린다. 둘 다
    제약 조건을 바꿔 버리는 자리라 평문으로 눌러 버릴 수 없다.

    표 상태를 스택으로 두는 이유는 셀 안에 표가 또 들어 있는 지문이 있기
    때문이다(3340번 외 10개). 상태가 하나뿐이면 안쪽 ``<tr>``이 바깥 행을
    덮어써서, 바깥 표가 해체되고 라벨이 엉뚱한 표에 붙는다.
    """

    def __init__(self, figures: FigureCounter | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self._figures = figures
        self._chunks: list[str] = []
        self._in_pre = False
        # 지문이 아닌 것들. HTMLParser는 이 안의 내용도 handle_data로 준다.
        self._muted = 0
        # 셀 안의 글자는 본문으로 바로 흘리지 않고 모았다가 행 단위로 낸다.
        # 태그 사이 줄바꿈이 그대로 섞이면 빈 칸이 생겨 열이 밀린다.
        self._tables: list[_Table] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _MUTED_TAGS:
            self._muted += 1
        elif self._muted:
            return
        elif tag == "br":
            self._emit("\n")
        elif tag == "pre":
            self._in_pre = True
            self._emit(f"\n{_PROTECTED}")
        elif tag == "table":
            self._tables.append(_Table())
        elif tag == "tr":
            # ``</tr>`` 없이 다음 ``<tr>``이 오는 지문이 있다(17432번). 모아 둔
            # 행을 먼저 내보내지 않으면 그 행이 통째로 사라진다.
            table = self._current_table()
            self._close_cell(table)
            self._close_row(table)
            table.row = []
        elif tag in ("td", "th"):
            table = self._current_table()
            self._close_cell(table)
            if table.row is None:
                # ``<tr>`` 없이 ``<th>``만 늘어놓은 머리글이 있다(2545번 등
                # 서브태스크 표). 행을 열어 주지 않으면 머리글이 한 칸씩
                # 세로로 쪼개져 어느 값이 어느 열인지 알 수 없게 된다.
                table.row = []
            table.cell = []
        elif tag in _BLOCK_TAGS:
            self._emit("\n")
        elif tag == "sup":
            self._emit("^")
        elif tag == "sub":
            self._emit("_")
        elif tag == "img":
            self._emit(self._image_text(dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag in _MUTED_TAGS:
            self._muted = max(0, self._muted - 1)
        elif self._muted:
            return
        elif tag == "pre":
            self._close_pre()
        elif tag in ("td", "th"):
            self._close_cell(self._current_table())
        elif tag == "tr":
            self._close_row(self._current_table())
        elif tag == "table":
            self._close_table()
        # 줄만 바꾸는 태그는 닫을 때 아무것도 넣지 않는다. 다음 여는 태그가
        # 이미 줄을 바꾸므로, 여기서 또 넣으면 목록과 표가 빈 줄로 벌어진다.
        elif tag in _PARAGRAPH_TAGS:
            self._emit("\n\n")

    def handle_data(self, data: str) -> None:
        if self._muted:
            return
        if self._in_pre:
            self._emit(data)
            return
        # 줄바꿈은 소스 포매팅일 뿐이다. 문단 구분은 블록 태그가 담당한다.
        collapsed = re.sub(r"\s+", " ", data)
        if collapsed.strip() or collapsed == " ":
            self._emit(collapsed)

    def close(self) -> None:
        # 닫는 태그가 빠진 지문이 있다. 모아 둔 셀을 버리면 표가 통째로 사라진다.
        super().close()
        self._close_pre()
        while self._tables:
            self._close_table()

    def _current_table(self) -> _Table:
        # ``<table>`` 없이 ``<tr>``만 있는 조각도 들어온다. 만들어 준다.
        if not self._tables:
            self._tables.append(_Table())
        return self._tables[-1]

    def _close_table(self) -> None:
        table = self._tables[-1]
        self._close_cell(table)
        self._close_row(table)
        self._tables.pop()

    def _close_pre(self) -> None:
        if not self._in_pre:
            return
        self._in_pre = False
        self._emit(f"{_PROTECTED}\n\n")

    def _emit(self, text: str) -> None:
        """가장 안쪽에서 열려 있는 셀이 받는다. 없으면 본문으로 간다."""
        for table in reversed(self._tables):
            if table.cell is not None:
                table.cell.append(text)
                return
        self._chunks.append(text)

    def _close_cell(self, table: _Table) -> None:
        if table.cell is None:
            return
        cell, table.cell = _flatten_cell("".join(table.cell)), None
        if table.row is None:
            # ``<tr>`` 없이 떠 있는 셀. 버리지 말고 한 줄로 내보낸다.
            self._emit(f"\n{cell}")
            return
        table.row.append(cell)

    def _close_row(self, table: _Table) -> None:
        if table.row is None:
            return
        row, table.row = table.row, None
        if row:
            self._emit("\n" + _CELL_SEPARATOR.join(row))

    def _image_text(self, attrs: dict[str, str | None]) -> str:
        if self._figures is None:
            return ""
        source = attrs.get("src") or ""
        return f" {self._figures.add(source)} "

    @property
    def text(self) -> str:
        return _tidy("".join(self._chunks))


def html_to_text(html: str, *, figures: FigureCounter | None = None) -> str:
    """지문 HTML 한 조각을 텍스트로 옮긴다.

    ``figures``를 주면 ``<img>`` 자리에 ``[그림 N]``을 남기고 원본 주소를
    모은다. 그림이 문장 어디에 걸리는지가 사라지면 전사를 읽어도 어느
    대목인지 알 수 없다.
    """
    extractor = _TextExtractor(figures)
    # 입력에 NUL이 섞이면 ``<pre>`` 보호 구간 판정(홀짝)이 그 뒤로 통째로
    # 뒤집힌다. 수집본에는 지금 하나도 없지만, 불변식을 운에 맡길 이유가 없다.
    extractor.feed((html or "").replace(_PROTECTED, ""))
    extractor.close()
    return extractor.text


def _flatten_cell(text: str) -> str:
    """셀 한 칸을 한 줄로 만든다.

    셀 안에 문단이 여럿이면 줄바꿈이 남고, 그 줄바꿈이 행을 쪼갠다. 쪼개진
    조각은 옆 열의 값과 한 줄에 붙어 **다른 표가 된다**(2453번). ``<pre>``
    구간만은 그대로 둔다 — 거기서는 줄바꿈이 곧 내용이다.
    """
    parts = text.split(_PROTECTED)
    flattened = [
        part if index % 2 else re.sub(r"\s*\n\s*", " ", part) for index, part in enumerate(parts)
    ]
    return _PROTECTED.join(flattened).strip()


def _tidy(text: str) -> str:
    """줄 끝 공백을 없애고 빈 줄이 두 줄 넘게 이어지지 않게 한다.

    ``<pre>`` 구간(:data:`_PROTECTED` 로 감싼 부분)은 건드리지 않는다. 격자
    지도·아스키 그림·의사코드가 거기 있어서 들여쓰기가 곧 내용이다.
    """
    parts = text.replace("\r\n", "\n").split(_PROTECTED)
    tidied = [
        part.strip("\n")
        if index % 2  # 홀수 번째 조각이 <pre> 안이다.
        else re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in part.split("\n")))
        for index, part in enumerate(parts)
    ]
    # 앞뒤 빈 줄만 턴다. ``strip()``을 쓰면 ``<pre>``로 시작하는 글의 첫 줄
    # 들여쓰기가 같이 날아간다.
    return "".join(tidied).strip("\n")


# -------------------------------------------------------------------- 적재


@dataclass(frozen=True)
class BojImport:
    """수집본에서 가져온 문제 하나.

    ``problem.figures[i]``와 ``images[i]``가 같은 그림을 가리킨다. 짝이
    맞지 않으면 ``images``를 비우고 ``notes``에 적는다.
    """

    problem: Problem
    images: tuple[Path, ...] = ()
    notes: tuple[str, ...] = ()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """수집본을 읽기 전용으로 연다.

    ``mode=ro``는 **DB 내용**을 지킨다. 수집본 폴더에 ``-wal``·``-shm``이
    생기는 것까지 막지는 못한다. 그것을 막는 ``immutable=1``은 쓰지 않는다 —
    WAL에 내용이 남은 순간 그것을 무시하고 **옛날 데이터를 조용히** 읽는다.
    수집기를 다시 돌린 직후가 정확히 그 상황이다.

    경로는 퍼센트 인코딩한다. 폴더 이름에 ``%``나 ``?``가 있으면 질의문자열이
    쪼개져 ``mode=ro``가 뒤로 밀리고, 그때는 쓰기 가능으로 열린다.
    """
    path = Path(db_path) if db_path is not None else BOJ_DB_PATH
    if not path.is_file():
        raise BojUnavailable(
            f"백준 수집본을 찾지 못했습니다: {path} · "
            "boj_problems/boj_problems.db 를 두거나 TCGEN_BOJ_DIR 를 설정하세요."
        )
    uri = f"file:{quote(path.as_posix())}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise BojUnavailable(f"백준 수집본을 열지 못했습니다: {error}") from error
    try:
        # sqlite3.connect는 지연 연결이다. 여기서 한 번 읽어 두지 않으면
        # 열기 실패가 호출부의 첫 질의에서 처리되지 않은 500으로 터진다.
        connection.execute("SELECT 1")
    except sqlite3.Error as error:
        connection.close()  # 실패한 연결을 그대로 두면 핸들이 샌다.
        raise BojUnavailable(f"백준 수집본을 열지 못했습니다: {error}") from error
    return connection


def load_boj_problem(
    problem_id: int,
    *,
    db_path: Path | None = None,
    images_dir: Path | None = None,
) -> BojImport:
    """문제 번호 하나로 :class:`~tcgen.models.Problem` 을 만든다."""
    if not BOJ_MIN_PROBLEM_ID <= problem_id <= BOJ_MAX_PROBLEM_ID:
        raise BojError(
            f"문제 번호가 범위를 벗어났습니다: {problem_id} "
            f"({BOJ_MIN_PROBLEM_ID}~{BOJ_MAX_PROBLEM_ID})"
        )

    # ``sqlite3.Connection``의 컨텍스트 관리자는 트랜잭션만 끝내고 연결은 닫지
    # 않는다. 그대로 두면 1.4GB 수집본 핸들이 가져올 때마다 쌓이고, 사용자는
    # 수집본을 바꾸려 할 때 '사용 중'으로 막힌다.
    with closing(connect(db_path)) as connection:
        try:
            row = connection.execute(
                "SELECT title, time_limit, memory_limit FROM problems WHERE problem_id = ?",
                (problem_id,),
            ).fetchone()
            if row is None:
                raise BojNotFound(
                    f"수집본에 없는 문제입니다: {problem_id} · "
                    "비공개이거나 수집 당시 받지 못한 문제일 수 있습니다."
                )
            sections = connection.execute(
                "SELECT section_id, heading, body_html FROM problem_sections"
                " WHERE problem_id = ? ORDER BY sequence",
                (problem_id,),
            ).fetchall()
            samples = connection.execute(
                "SELECT input_text, output_text FROM problem_samples"
                " WHERE problem_id = ? ORDER BY sample_no",
                (problem_id,),
            ).fetchall()
        except sqlite3.Error as error:
            # 스키마가 다르거나 파일이 손상된 수집본. 여기서 잡지 않으면
            # 처리되지 않은 500이 되어 원인이 사용자에게 가지 않는다.
            raise BojUnavailable(f"수집본을 읽지 못했습니다: {error}") from error

    title, time_text, memory_text = row
    counter = FigureCounter()
    fields, notes = _collect_sections(sections, counter)
    limits, limit_notes = _build_limits(time_text, memory_text)
    images, image_notes = _match_images(problem_id, len(counter.sources), images_dir)

    problem = Problem(
        slug=f"{SLUG_PREFIX}{problem_id}",
        title=str(title or f"{problem_id}번"),
        statement=fields["statement"],
        input_spec=fields["input_spec"],
        output_spec=fields["output_spec"],
        constraints=fields["constraints"],
        hints=fields["hints"],
        examples=tuple(_build_examples(samples)),
        figures=tuple(
            # 전사 없는 그림을 decorative로 두면 그 그림이 정한 규칙이 조용히
            # 사라진다. 꼭 필요한 것으로 보고 시작해, 사람이 보고 내린다.
            Figure(ref=figure_ref(number), role=FIGURE_ROLE_SPEC)
            for number in range(1, len(counter.sources) + 1)
        ),
        limits=limits,
    )
    return BojImport(
        problem=problem,
        images=images,
        notes=tuple(notes) + limit_notes + image_notes,
    )


def source_url(problem_id: int) -> str:
    return SOURCE_URL.format(problem_id=problem_id)


def import_into_workspace(
    problem_id: int,
    *,
    db_path: Path | None = None,
    images_dir: Path | None = None,
    base_dir: Path | None = None,
) -> tuple[Workspace, tuple[str, ...]]:
    """수집본의 문제 하나로 작업공간을 만든다.

    그림 파일은 작업공간 안으로 복사한다. 작업 폴더만 들고 다녀도 그림을 볼
    수 있어야 하고(PRD 6.3), 원본 수집본은 쓰기 대상이 아니기 때문이다.
    ``[그림 N]``은 ``assets/figures/figure-N.*``에 대응한다.
    """
    imported = load_boj_problem(problem_id, db_path=db_path, images_dir=images_dir)
    workspace = create_workspace(imported.problem, base_dir=base_dir)
    notes = imported.notes + _copy_figures(workspace, imported.images)
    return workspace, notes


def _copy_figures(workspace: Workspace, images: Sequence[Path]) -> tuple[str, ...]:
    if not images:
        return ()
    target = workspace.figures_dir
    # 쓰기 직전에 봉쇄를 재확인한다. ``clear_cases``가 rmtree 앞에서 하는 것과
    # 같은 이유다. ``assets``가 링크로 바뀌어 있으면 그 바깥에 쓰게 된다.
    if not workspace.contains(target):
        raise BojError(f"작업공간 밖에 쓰려 했습니다: {target}")
    target.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for number, source in enumerate(images, start=1):
        try:
            shutil.copyfile(source, target / f"figure-{number}{source.suffix.lower()}")
        except OSError as error:
            # 복사가 실패해도 지문은 이미 멀쩡하다. 여기서 멈추는 것보다
            # 어느 그림이 비었는지 알려 주는 쪽이 낫다.
            failed.append(f"{figure_ref(number)} 복사 실패: {error}")
    return tuple(failed)


def _collect_sections(
    rows: Sequence[tuple[str, str | None, str]], counter: FigureCounter
) -> tuple[dict[str, str], list[str]]:
    """섹션을 문제 필드에 나눠 담는다. 어느 섹션도 버리지 않는다."""
    statement: list[str] = []
    input_spec: list[str] = []
    output_spec: list[str] = []
    constraints: list[str] = []
    hints: list[str] = []
    notes: list[str] = []

    for section_id, heading, body_html in rows:
        if body_html and len(body_html) > MAX_SECTION_HTML_BYTES:
            # 지문 한 섹션이 2MB를 넘을 일은 없다. 변환은 조각을 메모리에
            # 쌓으므로 통째로 밀어 넣으면 서버가 작업 중인 다른 문제까지 잃는다.
            notes.append(
                f"'{section_id}' 섹션이 비정상적으로 큽니다"
                f"({len(body_html):,}자). 읽지 않고 건너뛰었습니다."
            )
            continue
        text = html_to_text(body_html, figures=counter)
        if not text:
            continue
        if section_id == SECTION_STATEMENT:
            statement.append(text)
        elif section_id == SECTION_INPUT:
            input_spec.append(text)
        elif section_id == SECTION_OUTPUT:
            output_spec.append(text)
        elif section_id == SECTION_LIMIT or section_id.startswith(_CONSTRAINT_PREFIX):
            constraints.append(_labelled(heading, text, bare=section_id == SECTION_LIMIT))
        else:
            # 인터랙션 규약·채점 방식 같은 것들이 여기로 온다. 이름을 붙여
            # 두지 않으면 나중에 무엇을 읽고 있는지 알 수 없다.
            hints.append(_labelled(heading, text))

    # 둘 다 비었을 때만 알리면, 출력 설명 하나만 빈 문제(인터랙티브 등)가
    # 조용히 지나간다. 빈 칸을 알리지 않으면 사람은 다 채워진 줄 안다.
    for label, parts in (
        ("문제 설명", statement),
        ("입력 설명", input_spec),
        ("출력 설명", output_spec),
    ):
        if not parts:
            notes.append(f"{label}이 수집본에 없습니다. 직접 채워 주세요.")

    collected = {
        "statement": statement,
        "input_spec": input_spec,
        "output_spec": output_spec,
        "constraints": constraints,
        "hints": hints,
    }
    return {key: _capped(key, parts, notes) for key, parts in collected.items()}, notes


def _capped(key: str, parts: list[str], notes: list[str]) -> str:
    """필드를 상한까지만 담는다. 잘랐으면 반드시 적는다.

    폼 입력에는 상한이 있는데 이쪽에만 없으면, 상한을 우회하는 입구가 하나
    열려 있는 것과 같다.
    """
    joined = "\n\n".join(parts)
    limit = FIELD_LIMITS[key]
    if len(joined) <= limit:
        return joined
    notes.append(f"'{key}'가 상한을 넘어 잘렸습니다({len(joined):,}자 → {limit:,}자).")
    return joined[:limit]


def _labelled(heading: str | None, text: str, *, bare: bool = False) -> str:
    label = (heading or "").strip()
    return text if bare or not label else f"[{label}]\n{text}"


def _build_limits(time_text: str | None, memory_text: str | None) -> tuple[Limits, tuple[str, ...]]:
    time_ms = parse_time_limit(time_text)
    memory_mb = parse_memory_limit(memory_text)
    notes: list[str] = []
    if time_ms is None:
        notes.append(
            f"시간 제한을 읽지 못했습니다({time_text!r}). "
            f"{DEFAULT_TIME_LIMIT_MS}ms로 가정했습니다."
        )
    if memory_mb is None:
        notes.append(
            f"메모리 제한을 읽지 못했습니다({memory_text!r}). "
            f"{DEFAULT_MEMORY_LIMIT_MB}MB로 가정했습니다."
        )
    limits = Limits(
        time_ms=time_ms if time_ms is not None else DEFAULT_TIME_LIMIT_MS,
        memory_mb=memory_mb if memory_mb is not None else DEFAULT_MEMORY_LIMIT_MB,
        assumed=time_ms is None or memory_mb is None,
    )
    return limits, tuple(notes)


def _build_examples(rows: Iterable[tuple[str | None, str | None]]) -> Iterable[Example]:
    """예제를 LF로 맞춰 담는다.

    수집본은 CRLF를 그대로 갖고 있다. 출력 대조는 양쪽을 정규화하므로
    무사하지만, 입력은 그대로 stdin에 들어가 한 글자씩 읽는 풀이를 깨뜨린다.
    """
    for input_text, output_text in rows:
        yield Example(
            input=_to_lf(input_text),
            output=_to_lf(output_text),
        )


def _to_lf(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _match_images(
    problem_id: int, figure_count: int, images_dir: Path | None
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """그림 번호와 내려받아 둔 파일을 위치로 짝짓는다.

    수집본은 파일 이름에 원본 주소를 남기지 않았다. 등장 순서가 유일한
    단서이므로, 개수가 어긋나면 짝짓지 않는다. 한 칸 밀린 그림은 비어 있는
    그림보다 나쁘다.
    """
    if figure_count == 0:
        return (), ()
    root = Path(images_dir) if images_dir is not None else BOJ_IMAGES_DIR
    folder = root / str(problem_id)
    files = _numbered_images(folder, root)
    # 개수만 맞으면 짝이 맞는 것으로 보면 안 된다. 파일이 ``1.png``, ``3.png``
    # 두 개인 채로 그림도 2개면 ``[그림 2]``에 세 번째 그림이 묶인다. 수집기를
    # 다시 돌려 일부만 실패하면 바로 그 상태가 된다.
    if [_image_number(path) for path in files] == list(range(1, figure_count + 1)):
        return files, ()
    found = ", ".join(path.name for path in files) or "없음"
    note = (
        f"지문의 그림은 {figure_count}개인데 내려받아 둔 파일은 {len(files)}개({found})입니다. "
        f"짝이 틀어지지 않도록 자동 연결을 생략했습니다 ({folder})."
    )
    return (), (note,)


def _image_number(path: Path) -> int:
    match = _IMAGE_NAME.match(path.name)
    return int(match.group(1)) if match else 0


def _numbered_images(folder: Path, root: Path) -> tuple[Path, ...]:
    """``images/<번호>/`` 안의 그림 파일을 순번대로 돌려준다.

    링크는 받지 않는다. 수집본은 남에게 받아 푸는 1.9GB 압축 파일이고,
    Windows에서는 junction과 하드링크를 관리자 권한 없이 만들 수 있다.
    ``is_file()``과 ``copyfile``은 둘 다 링크를 따라가므로, 이름만 검사하면
    ``1.png``라는 이름의 링크 하나로 홈 디렉터리의 아무 파일이나 작업공간
    안으로 복사된다.

    ``is_symlink()``는 Windows junction에 ``False``를 돌려준다. 그래서 링크
    여부를 묻지 않고 **실체 경로가 ``images`` 안에 남아 있는지**를 본다.
    """
    try:
        if not folder.resolve().is_relative_to(root.resolve()):
            return ()
    except OSError:
        return ()
    if folder.is_symlink() or not folder.is_dir():
        return ()
    matched: list[tuple[int, Path]] = []
    for path in folder.iterdir():
        match = _IMAGE_NAME.match(path.name)
        if match is None or path.is_symlink():
            continue
        try:
            info = path.lstat()  # 링크를 따라가지 않고 실체만 본다.
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FIGURE_BYTES:
            continue
        if info.st_nlink > 1:
            # 하드링크. 실체가 수집본 밖에 있을 수 있고, 링크인지 아닌지를
            # 경로만으로는 구별할 수 없다.
            continue
        matched.append((int(match.group(1)), path))
    return tuple(path for _, path in sorted(matched)[:MAX_FIGURES])
