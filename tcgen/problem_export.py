"""문제 가져오기용 zip: N/problem.json + N/testcase/*.in, *.out.

첨부된 문제 묶음의 스키마를 따른다. 그림은 N/images/<번호>.png로 함께 넣고
HTML의 원래 참조 위치에서 zip 안의 상대 경로로 참조한다.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from html import escape
from pathlib import Path, PurePosixPath

from PIL import Image

from tcgen import compare, exporter, images, statement_html
from tcgen.config import DIFFICULTY_LEVELS
from tcgen.workspace import Workspace

SOURCE = "Baekjoon Online Judge"
FORMAT = "problem"
REQUIRED = {"display_id", "title", "description", "tags", "input_description", "output_description",
            "test_case_score", "hint", "time_limit", "memory_limit", "samples", "template", "spj",
            "rule_type", "source", "field", "difficulty", "answers"}
_IMAGE = re.compile(r"\[그림\s*(\d+)\]")
#: 원문 표시 너비를 모르는 그림(직접 첨부 등)의 최대 표시 너비(px).
DEFAULT_FIGURE_WIDTH = 400
_IMAGE_SRC = re.compile(r'<img\b[^>]*src=[\'\"](images/\d+\.png)[\'\"]', re.I)


def destination(workspace: Workspace) -> Path:
    return workspace.export_dir / f"{workspace.slug}_problem.zip"


def legacy_destination(workspace: Workspace) -> Path:
    return workspace.export_dir / f"{workspace.slug}_testcases.zip"


def current_archive(workspace: Workspace) -> Path:
    new = destination(workspace)
    return new if new.is_file() else legacy_destination(workspace)


def remove_archives(workspace: Workspace) -> None:
    for path in (destination(workspace), legacy_destination(workspace)):
        path.unlink(missing_ok=True)


def _display_width(original: int, data: bytes) -> int:
    """원문 표시 너비를 쓴다. 모르면 원본 크기로 두되 지나치게 크지 않게 줄인다."""
    if original > 0:
        return original
    with Image.open(io.BytesIO(data)) as image:
        return min(image.width, DEFAULT_FIGURE_WIDTH)


def section_texts(problem) -> dict[str, str]:
    """내보낼 지문 섹션의 원문. 제한 조건은 입력 설명 뒤에 붙인다."""
    input_text = problem.input_spec
    if problem.constraints and problem.constraints not in input_text:
        input_text += "\n\n" + problem.constraints
    return {"description": problem.statement, "input_description": input_text,
            "output_description": problem.output_spec}


def build_payload(workspace: Workspace, metadata: dict) -> tuple[dict, dict[str, bytes]]:
    problem = workspace.load_problem()
    used = set()
    embedded = {}
    figures = {}

    def figure(number: int) -> str:
        if number not in embedded:
            if not 1 <= number <= len(problem.figures):
                raise exporter.ExportError(f"[그림 {number}]에 대응하는 첨부 그림이 없습니다.")
            path = images.find(workspace, number)
            if path is None:
                raise exporter.ExportError(f"[그림 {number}] 파일이 없습니다. 그림을 첨부하거나 Drive에서 가져와 주세요.")
            name = f"images/{number}.png"
            figures[name] = images.normalize(path.read_bytes())
            width = _display_width(problem.figures[number - 1].width, figures[name])
            embedded[number] = (f'<img src="{name}" alt="그림 {number}" width="{width}" '
                                'style="max-width:100%;height:auto" />')
        used.add(number)
        return embedded[number]

    def normalize_refs(text: str) -> str:
        for number, item in enumerate(problem.figures, 1):
            if item.ref and item.ref != f"[그림 {number}]":
                text = text.replace(item.ref, f"[그림 {number}]")
        return text

    def html(text: str, formatted: str | None = None) -> dict:
        if formatted:
            # 서식 HTML은 허용 목록으로 정리하고 원문 보존을 확인한 것이다.
            return {"format": "html", "value": _IMAGE.sub(lambda match: figure(int(match.group(1))),
                                                          normalize_refs(formatted))}
        # 사용자 지문을 HTML로 실행하지 않는다. 그림 자리만 앱이 만든 img로 치환한다.
        text = normalize_refs(str(text or ""))
        if not text.strip():
            return {"format": "html", "value": ""}
        paragraphs = []
        for paragraph in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").strip("\n")):
            if not paragraph.strip():
                continue
            parts, start = [], 0
            for match in _IMAGE.finditer(paragraph):
                parts.append(escape(paragraph[start:match.start()]))
                parts.append(figure(int(match.group(1))))
                start = match.end()
            parts.append(escape(paragraph[start:]))
            content = "".join(parts)
            # 원문의 들여쓰기·아스키 그림을 보존하면서 일반 문단은 편집기와 같은 p 태그로 보낸다.
            content = re.sub(r"(?m)^( +)", lambda match: "&nbsp;" * len(match.group(1)), content)
            paragraphs.append("<p>" + content.replace("\n", "<br />") + "</p>")
        return {"format": "html", "value": "".join(paragraphs)}

    texts = section_texts(problem)
    styled = metadata.get("statement_html") or {}
    # 지문을 고친 뒤라면 예전 서식은 쓰지 않는다.
    formatted = (styled.get("sections") or {}) if styled.get("source_key") == statement_html.source_key(texts) else {}
    description = html(texts["description"], formatted.get("description"))
    input_description = html(texts["input_description"], formatted.get("input_description"))
    output_description = html(texts["output_description"], formatted.get("output_description"))
    # 힌트는 원문에 있어도 내보내지 않는다. 형식상 필수인 필드는 빈 HTML로 둔다.
    hint = {"format": "html", "value": ""}
    hint_images = {int(match.group(1)) for match in _IMAGE.finditer(problem.hints)}
    hint_images.update(number for number, item in enumerate(problem.figures, 1)
                       if item.ref and item.ref in problem.hints)
    # 수동 첨부만 한 그림은 본문 아래에 두되 [그림 N]으로 원하는 위치를 지정할 수 있다.
    for number in range(1, len(problem.figures) + 1):
        if number not in used and number not in hint_images:
            description["value"] += f"<p>{figure(number)}</p>"
    numbers = exporter.collect_case_numbers(workspace.cases_dir)
    if {record.index for record in workspace.load_cases_index()} != set(numbers):
        raise exporter.ExportError("케이스 파일과 검증 목록이 다릅니다. 검증되지 않은 파일을 추가로 내보낼 수 없습니다.")
    return {"display_id": str(problem.boj_id) if problem.boj_id is not None else problem.slug,
            "title": problem.title, "description": description, "tags": list(metadata["tags"]),
            "input_description": input_description, "output_description": output_description,
            "test_case_score": [{"score": 100, "input_name": f"{n}.in", "output_name": f"{n}.out"} for n in numbers],
            "hint": hint, "time_limit": problem.limits.time_ms, "memory_limit": problem.limits.memory_mb,
            "samples": metadata["samples"], "template": {}, "spj": None, "rule_type": "ACM",
            "source": SOURCE, "field": metadata["field"], "difficulty": metadata["difficulty"], "answers": []}, figures


def _validate_payload(payload: dict) -> list[str]:
    errors = []
    if not isinstance(payload, dict) or not REQUIRED <= payload.keys():
        return ["problem.json에 필수 항목이 없습니다."]
    if not isinstance(payload["display_id"], str) or not payload["display_id"].strip():
        errors.append("문제 번호가 비어 있습니다.")
    elif len(payload["display_id"]) > 24:
        errors.append("code-place는 문제 번호를 24자로 자릅니다. 번호를 24자 이하로 지정해 주세요.")
    if not isinstance(payload["title"], str) or not payload["title"].strip():
        errors.append("문제 제목이 비어 있습니다.")
    elif len(payload["title"]) > 128:
        errors.append("code-place 가져오기는 문제 제목을 128자까지 허용합니다.")
    for name in ("description", "input_description", "output_description", "hint"):
        item = payload[name]
        if not isinstance(item, dict) or item.get("format") != "html" or not isinstance(item.get("value"), str):
            errors.append(f"{name}의 HTML 형식이 잘못되었습니다.")
        elif name != "hint" and not item["value"].strip():
            errors.append(f"{name}이 비어 있습니다.")
        elif re.search(r'<img\b[^>]*src=[\'\"](?!images/\d+\.png[\'\"])', item.get("value", ""), re.I):
            errors.append("본문에 외부 경로 또는 로컬 파일을 참조하는 그림이 있습니다.")
    if type(payload["field"]) is not int or payload["field"] not in range(6):
        errors.append("문제 영역은 0~5 정수여야 합니다.")
    if not isinstance(payload["difficulty"], str) or payload["difficulty"] not in DIFFICULTY_LEVELS:
        errors.append("난이도는 VeryLow/Low/Mid/High/VeryHigh여야 합니다.")
    if not isinstance(payload["tags"], list) or not 1 <= len(payload["tags"]) <= 3 or any(not isinstance(tag, str) or not tag.strip() for tag in payload["tags"]):
        errors.append("태그는 1~3개의 문자열이어야 합니다.")
    if not isinstance(payload["samples"], list) or not payload["samples"] or any(
        not isinstance(item, dict) or not isinstance(item.get("input"), str) or not isinstance(item.get("output"), str)
        or not item["input"] or not item["output"] for item in payload["samples"]
    ):
        errors.append("예제 입출력은 빈 문자열이 아닌 1쌍 이상이어야 합니다.")
    for name, maximum in (("time_limit", 60000), ("memory_limit", 1024)):
        if type(payload[name]) is not int or not 1 <= payload[name] <= maximum:
            errors.append(f"code-place의 {name} 허용 범위는 1~{maximum}입니다.")
    if (payload["template"] != {} or payload["spj"] is not None or payload["rule_type"] != "ACM"
            or payload["source"] != SOURCE or payload["answers"] != []):
        errors.append("고정 메타데이터 값이 규격과 다릅니다.")
    cases = payload["test_case_score"]
    if not isinstance(cases, list) or not cases:
        errors.append("test_case_score가 비어 있습니다.")
    else:
        for number, item in enumerate(cases, 1):
            if item != {"score": 100, "input_name": f"{number}.in", "output_name": f"{number}.out"}:
                errors.append("테스트케이스 이름·점수가 잘못되었거나 번호가 연속이 아닙니다.")
                break
    return errors


def encode_problem_json(payload: dict) -> str:
    r"""한글은 표준 JSON 이스케이프, source는 영문·공백까지 \uXXXX로 기록한다."""
    text = json.dumps(payload, ensure_ascii=True, indent=4)
    encoded = payload["source"].encode("utf-16-be")
    source = "".join(f"\\u{int.from_bytes(encoded[index:index + 2], 'big'):04x}"
                     for index in range(0, len(encoded), 2))
    original = f'    "source": {json.dumps(payload["source"], ensure_ascii=True)}'
    return text.replace(original, f'    "source": "{source}"', 1)


def export_problem(workspace: Workspace, metadata: dict) -> exporter.ExportResult:
    issues = exporter.inspect_cases(workspace.cases_dir)
    if compare.comparison_for(workspace.load_problem()).tolerant:
        issues += (exporter.ExportIssue(exporter.SEVERITY_WARNING,
            "실수 오차를 허용하는 문제입니다. 현재 내보내기 설정(spj=null)은 문자열 채점이므로, "
            "대상 채점기에도 실수 오차 비교 설정 또는 SPJ가 필요합니다."),)
    fatal = [issue.message for issue in issues if issue.severity == exporter.SEVERITY_ERROR]
    payload, figures = build_payload(workspace, metadata)
    fatal.extend(_validate_payload(payload))
    if fatal:
        raise exporter.ExportError(" / ".join(fatal))
    path = destination(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".part")
    try:
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("1/problem.json", encode_problem_json(payload))
            for name, data in figures.items():
                archive.writestr("1/" + name, data)
            for item in payload["test_case_score"]:
                for key in ("input_name", "output_name"):
                    archive.write(workspace.cases_dir / item[key], "1/testcase/" + item[key])
        verification = verify_problem_zip(staging)
        if not any(issue.severity == exporter.SEVERITY_ERROR for issue in verification):
            staging.replace(path)
        return exporter.ExportResult(path, len(payload["test_case_score"]), tuple(issues) + verification)
    finally:
        staging.unlink(missing_ok=True)


def verify_problem_zip(path: Path) -> tuple[exporter.ExportIssue, ...]:
    errors = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name for name in names):
                return (exporter.ExportIssue(exporter.SEVERITY_ERROR, "zip에 중복되거나 잘못된 파일 경로가 있습니다."),)
            files = {name for name in names if not name.endswith("/")}
            roots = {name.split("/")[0] for name in files}
            if not roots or roots != {str(n) for n in range(1, len(roots) + 1)}:
                return (exporter.ExportIssue(exporter.SEVERITY_ERROR, "문제 폴더 번호는 1부터 연속이어야 합니다."),)
            expected = set()
            for root in sorted(roots, key=int):
                metadata_path = root + "/problem.json"
                expected.add(metadata_path)
                try:
                    payload = json.loads(archive.read(metadata_path).decode("utf-8"))
                except (KeyError, UnicodeError, ValueError):
                    errors.append(f"{metadata_path}을 읽지 못했습니다.")
                    continue
                schema_errors = _validate_payload(payload)
                errors.extend(schema_errors)
                if schema_errors:
                    continue
                for item in payload["test_case_score"]:
                    for key in ("input_name", "output_name"):
                        name = root + "/testcase/" + item[key]
                        expected.add(name)
                        if name not in files:
                            errors.append(f"{name} 파일이 없습니다.")
                        else:
                            data = archive.read(name)
                            if b"\r\n" in data:
                                errors.append(f"{name}에 CRLF가 남아 있습니다.")
                html = "".join(payload[key]["value"] for key in ("description", "input_description", "output_description", "hint"))
                for src in _IMAGE_SRC.findall(html):
                    name = root + "/" + src
                    expected.add(name)
                    if name not in files:
                        errors.append(f"{name} 파일이 없습니다.")
                for sample in payload["samples"]:
                    if any("\r\n" in value for value in sample.values()):
                        errors.append("예제 입출력에 CRLF가 남아 있습니다.")
            if files - expected:
                errors.append("문제 zip에 규격 외 파일이 있습니다.")
            bad = archive.testzip()
            if bad:
                errors.append(f"zip CRC 검사 실패: {bad}")
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        errors.append(f"문제 zip을 읽지 못했습니다: {error}")
    return tuple(exporter.ExportIssue(exporter.SEVERITY_ERROR, error) for error in errors)


def bundle(paths: list[Path], target: Path) -> None:
    """개별 문제 zip을 첨부 예시와 동일한 다중 문제 zip으로 합친다."""
    if not paths:
        raise exporter.ExportError("묶어 내보낼 문제 zip이 없습니다.")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".part")
    try:
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as output:
            index = 0
            for path in paths:
                issues = verify_problem_zip(path)
                if issues:
                    raise exporter.ExportError(f"{path.name}: {issues[0].message}")
                with zipfile.ZipFile(path) as source:
                    roots = sorted({name.split("/")[0] for name in source.namelist() if not name.endswith("/")}, key=int)
                    for root in roots:
                        index += 1
                        prefix = root + "/"
                        payload = json.loads(source.read(prefix + "problem.json"))
                        hint_images = set(_IMAGE_SRC.findall(payload["hint"]["value"]))
                        content_images = {name for key in ("description", "input_description", "output_description")
                                          for name in _IMAGE_SRC.findall(payload[key]["value"])}
                        hint_only_images = hint_images - content_images
                        payload["hint"] = {"format": "html", "value": ""}
                        for name in source.namelist():
                            if name.startswith(prefix) and not name.endswith("/"):
                                relative = name[len(prefix):]
                                if relative == "problem.json":
                                    output.writestr(f"{index}/problem.json", encode_problem_json(payload))
                                elif relative not in hint_only_images:
                                    output.writestr(str(index) + "/" + relative, source.read(name))
        issues = verify_problem_zip(staging)
        if issues:
            raise exporter.ExportError(issues[0].message)
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)
