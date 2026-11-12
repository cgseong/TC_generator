"""단계별 오케스트레이션 (PRD 11장).

각 단계는 독립적으로 호출할 수 있다. 위저드 모드는 한 단계씩 부르고 사람이
확인하며, 전문가 모드는 :meth:`Pipeline.run_all`로 이어서 돌린다.

게이트:
- 정답 코드가 확정되기 전에는 케이스를 만들지 않는다 (F2-B6).
- 정답 코드가 확정되기 전에는 내보내지 않는다 (F3-7).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from tcgen import compare, exporter, history, images, problem_export, statement_html
from tcgen.config import (
    DEFAULT_FIX_ATTEMPTS,
    DEFAULT_REINFORCE_ROUNDS,
    MAX_STRESS_ROUNDS,
    MIN_CASE_COUNT,
)
from tcgen.engine import finalize, generate, metadata, prompts, report, strength
from tcgen.engine.events import LEVEL_ERROR, LEVEL_SUCCESS, LEVEL_WARN, EventBus
from tcgen.engine.execution import Executor, SafetyBlocked, write_script
from tcgen.engine.solution import (
    Counterexample,
    ExampleCheck,
    StressOutcome,
    check_determinism,
    check_examples,
    run_stress,
)
from tcgen.jobs import Cancelled
from tcgen.llm import LLMClient, LLMError
from tcgen.models import Example, Figure, Limits, Problem
from tcgen.runner.base import Runner
from tcgen.runner.env import EnvironmentReport, probe_environment
from tcgen.runner.local import LocalRunner
from tcgen.workspace import Workspace

logger = logging.getLogger(__name__)

STAGE_PARSE = "parse"
STAGE_SOLUTION = "solution"
STAGE_CASES = "cases"
STAGE_STRENGTH = "strength"
STAGE_EXPORT = "export"
STAGE_FINAL = "final"

STRESS_GENERATOR_NAME = "stress_gen"
_GENERATOR_KINDS = ("random", "scale", "anti")


class PipelineError(Exception):
    """단계 실행을 진행할 수 없는 상태."""


@dataclass(frozen=True)
class SolutionOutcome:
    """정답 확정 단계 결과."""

    confirmed: bool
    examples: ExampleCheck
    stress: StressOutcome
    attempts: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "examples": self.examples.to_dict(),
            "stress": self.stress.to_dict(),
            "attempts": self.attempts,
            "note": self.note,
        }


class Pipeline:
    """한 문제의 작업 흐름을 조율한다."""

    def __init__(
        self,
        workspace: Workspace,
        cli: LLMClient,
        *,
        runner: Runner | None = None,
        bus: EventBus | None = None,
        environment: EnvironmentReport | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.workspace = workspace
        self.cli = cli
        self.bus = bus or EventBus()
        self.environment = environment or probe_environment()
        self.executor = Executor(runner=runner or LocalRunner(), work_dir=workspace.root)
        self.cancel_event = cancel_event
        # 단계마다 HTTP 요청이 갈리면서 Pipeline이 새로 만들어진다. 상태를
        # 메모리에만 두면 위저드 모드 리포트가 통째로 비므로 디스크에서 읽는다.
        self._state: dict[str, Any] = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        path = self.workspace.reports_dir / report.RUN_FILENAME
        if not path.exists():
            return {}
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("이전 실행 기록을 읽지 못했습니다: %s", error)
            return {}
        return {key: value for key, value in stored.items() if value is not None}

    def _persist_state(self) -> None:
        """각 단계 산출물을 즉시 기록한다 (PRD NF-R2)."""
        try:
            report.write_run_json(self.workspace.reports_dir, self._run_payload())
        except OSError as error:
            logger.warning("실행 기록을 저장하지 못했습니다: %s", error)

    def _check_cancel(self) -> None:
        """중단 요청을 단계 경계에서 확인한다 (PRD 8.2).

        실행 중인 프로세스를 즉시 끊지는 않는다. 현재 케이스가 끝나면 멈춘다.
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled("사용자가 중단을 요청했습니다.")

    def _progress(self, stage: str, message: str, **data: Any) -> None:
        self._check_cancel()
        self.bus.emit(stage, message, **data)

    # ------------------------------------------------------------------ 문제

    @property
    def problem(self) -> Problem:
        return self.workspace.load_problem()

    def _save(self, problem: Problem) -> Problem:
        self.workspace.save_problem(problem)
        return problem

    def parse_statement(self, raw_text: str) -> Problem:
        """지문 전문을 구조화 항목으로 채운다 (PRD F1-2)."""
        self.bus.emit(STAGE_PARSE, "지문을 구조화하는 중입니다.")
        payload = self.cli.ask_json(prompts.parse_statement(raw_text), tag="parse")
        updated = self._apply_parsed(payload)
        self.bus.emit(
            STAGE_PARSE,
            f"지문 구조화 완료 (예제 {len(updated.examples)}개). 내용을 확인해 주세요.",
            LEVEL_SUCCESS,
        )
        return self._save_parsed(updated)

    def parse_pdf(self) -> Problem:
        """PDF 지문을 읽어 구조화 항목과 그림 전사를 채운다 (PRD F1-7).

        그림 파일 첨부와 함께 사용할 수 있다. 텍스트 붙여넣기와 달리 CLI가
        파일을 직접 읽으므로, 그림이 담은 정보가 사라지지 않는다.
        """
        pdf_path = self.workspace.source_pdf_path
        if not pdf_path.is_file():
            raise PipelineError(f"PDF 지문이 없습니다: {pdf_path}")

        self.bus.emit(STAGE_PARSE, "PDF 지문을 읽는 중입니다. 그림까지 확인합니다.")
        payload = self.cli.ask_json_about_files(
            prompts.parse_pdf(pdf_path), [pdf_path], tag="parse-pdf"
        )
        figures = tuple(
            Figure.from_dict(item)
            for item in payload.get("figures", []) or []
            if isinstance(item, dict)
        )
        updated = self._apply_parsed(payload, figures=figures, source_pdf=pdf_path.name)

        self.bus.emit(
            STAGE_PARSE,
            f"PDF 구조화 완료 (예제 {len(updated.examples)}개, 그림 {len(figures)}개). "
            "내용을 확인해 주세요.",
            LEVEL_SUCCESS,
        )
        unresolved = updated.unresolved_figures
        if unresolved:
            # 전사가 비면 그 그림의 정보는 아무도 모른다. 조용히 넘기면
            # 정답 코드와 브루트포스가 같은 오해를 공유한다.
            refs = ", ".join(figure.ref for figure in unresolved)
            self.bus.emit(
                STAGE_PARSE,
                f"풀이에 필요한 그림의 전사가 비어 있습니다({refs}). 직접 채워 주세요.",
                LEVEL_WARN,
                figures=[figure.to_dict() for figure in unresolved],
            )
        return self._save_parsed(updated)

    def _save_parsed(self, updated: Problem) -> Problem:
        self._save(updated)
        self.workspace.clear_cases()
        self._invalidate("examples", "stress", "cases", "strength", "final", "export")
        return updated

    def _apply_parsed(
        self,
        payload: dict[str, Any],
        *,
        figures: tuple[Figure, ...] | None = None,
        source_pdf: str | None = None,
    ) -> Problem:
        """파싱 결과를 현재 문제에 얹는다. 텍스트·PDF 두 경로가 함께 쓴다."""
        current = self.problem
        limits = Limits(
            time_ms=_as_int(payload.get("time_ms"), current.limits.time_ms),
            memory_mb=_as_int(payload.get("memory_mb"), current.limits.memory_mb),
            # 둘 중 하나라도 지문에 없으면 가정값이 섞인 것이다 (PRD F1-5).
            assumed=not (payload.get("time_ms") and payload.get("memory_mb")),
        )
        examples = tuple(
            Example(input=str(item.get("input", "")), output=str(item.get("output", "")))
            for item in payload.get("examples", []) or []
            if isinstance(item, dict)
        )
        return current.with_changes(
            title=str(payload.get("title") or current.title),
            statement=str(payload.get("statement") or current.statement),
            input_spec=str(payload.get("input_spec") or current.input_spec),
            output_spec=str(payload.get("output_spec") or current.output_spec),
            constraints=str(payload.get("constraints") or current.constraints),
            hints=str(payload.get("hints") or current.hints),
            examples=examples or current.examples,
            figures=current.figures if figures is None else figures,
            source_pdf=current.source_pdf if source_pdf is None else source_pdf,
            limits=limits,
            # 지문이 바뀌면 이전 문제로 받은 확정은 더 이상 유효하지 않다.
            solution_confirmed=False,
            interpretation_confirmed=False,
        )

    # ----------------------------------------------------------------- 정답

    def build_solution(
        self,
        author_source: str | None = None,
        *,
        max_attempts: int = DEFAULT_FIX_ATTEMPTS,
    ) -> SolutionOutcome:
        """정답 코드를 확보하고 브루트포스 대조로 확정한다 (PRD F2-B)."""
        if self.problem.unresolved_figures:
            self.describe_figures()
        problem = self.problem
        self._invalidate("examples", "stress", "cases", "strength", "final", "export")
        self.workspace.clear_cases()
        self._save(problem.with_changes(solution_confirmed=False))
        source_kind = "author" if author_source else "llm"
        self._write_solution(author_source or self._ask_solution(problem))
        self._write_helpers(problem)

        outcome = self._confirm_loop(problem, max_attempts)
        self._save(
            self.problem.with_changes(
                solution_source=source_kind,
                solution_confirmed=outcome.confirmed,
            )
        )
        self._state["stress"] = outcome.stress.to_dict()
        self._state["examples"] = outcome.examples.to_dict()
        self._persist_state()
        level = LEVEL_SUCCESS if outcome.confirmed else LEVEL_ERROR
        self.bus.emit(
            STAGE_SOLUTION,
            "정답 코드가 확정되었습니다."
            if outcome.confirmed
            else "정답 코드를 확정하지 못했습니다. 내보내기가 차단됩니다.",
            level,
        )
        return outcome

    def describe_figures(self) -> dict:
        problem = self.problem
        figures = list(problem.figures)
        changed = 0
        for number, figure in enumerate(figures, start=1):
            self._check_cancel()
            if figure.description.strip():
                continue
            path = images.find(self.workspace, number)
            if path is None:
                if figure.is_critical:
                    raise PipelineError(f"{figure.ref} 그림을 첨부하거나 전사를 직접 입력해 주세요.")
                continue
            # AVIF 등은 CLI가 직접 읽지 못할 수 있어 PNG로 통일한다.
            path = images.store(self.workspace, number, path.read_bytes())
            self.bus.emit(STAGE_PARSE, f"{figure.ref} 그림을 읽는 중입니다.")
            payload = self.cli.ask_json_about_files(
                prompts.describe_figure(problem, figure.ref, path), [path], tag=f"figure-{number}",
            )
            description = str(payload.get("description", "")).strip()
            if not description:
                raise PipelineError(f"{figure.ref} 그림을 읽지 못했습니다. 전사를 직접 입력해 주세요.")
            figures[number - 1] = Figure(ref=figure.ref, role=figure.role, description=description)
            changed += 1
            self._save(problem.with_changes(figures=tuple(figures), solution_confirmed=False))
        if changed:
            self.workspace.clear_cases()
            self._invalidate("examples", "stress", "cases", "strength", "final", "export")
        return {"described": changed}

    def _ask_solution(self, problem: Problem) -> str:
        self.bus.emit(STAGE_SOLUTION, "정답 코드를 생성하는 중입니다.")
        return self.cli.ask_code(prompts.solution(problem), tag="solution")

    def _write_solution(self, source: str) -> None:
        findings = write_script(self.workspace.solution_path, source)
        self._warn_findings(STAGE_SOLUTION, "정답 코드", findings)

    def _write_helpers(self, problem: Problem) -> None:
        self.bus.emit(STAGE_SOLUTION, "브루트포스·검증기·생성기를 준비하는 중입니다.")
        brute = self.cli.ask_code(prompts.brute_force(problem), tag="brute")
        self._warn_findings(
            STAGE_SOLUTION, "브루트포스", write_script(self.workspace.brute_path, brute)
        )
        validator = self.cli.ask_code(prompts.validator(problem), tag="validator")
        self._warn_findings(
            STAGE_SOLUTION, "검증기", write_script(self.workspace.validator_path, validator)
        )
        generator = self.cli.ask_code(prompts.stress_generator(problem), tag="stress-gen")
        self._warn_findings(
            STAGE_SOLUTION,
            "스트레스 생성기",
            write_script(self._stress_generator_path(), generator),
        )

    def _confirm_loop(self, problem: Problem, max_attempts: int) -> SolutionOutcome:
        rounds = min(problem.case_plan.stress_rounds, MAX_STRESS_ROUNDS)
        comparison = compare.comparison_for(problem)
        self.bus.emit(STAGE_SOLUTION, f"출력 비교: {comparison.describe()}")
        examples = ExampleCheck(passed=True)
        stress = StressOutcome(passed=False, rounds_run=0, note="아직 수행하지 않음")

        for attempt in range(1, max_attempts + 1):
            examples = check_examples(self.executor, self.workspace.solution_path, problem)
            if not examples.passed:
                self.bus.emit(
                    STAGE_SOLUTION,
                    f"[{attempt}/{max_attempts}] 예제 검증 실패: {examples.failures[0]}",
                    LEVEL_WARN,
                )
                if examples.counterexample is None or not self._apply_fix(
                    problem, examples.counterexample
                ):
                    break
                continue

            self.bus.emit(STAGE_SOLUTION, f"[{attempt}/{max_attempts}] 브루트포스 대조 {rounds}회 시작")
            stress = run_stress(
                self.executor,
                self.workspace.solution_path,
                self.workspace.brute_path,
                self._stress_generator_path(),
                rounds=rounds,
                limits=problem.limits,
                validator_path=self.workspace.validator_path,
                on_progress=self._stress_progress,
                comparison=comparison,
            )
            if stress.passed:
                note = self._determinism_note(problem)
                return SolutionOutcome(not bool(note), examples, stress, attempt, note=note)

            self.bus.emit(STAGE_SOLUTION, f"대조 실패: {stress.note or '반례 발견'}", LEVEL_WARN)
            if stress.counterexample is None or not self._apply_fix(
                problem, stress.counterexample
            ):
                break

        return SolutionOutcome(
            confirmed=False,
            examples=examples,
            stress=stress,
            attempts=max_attempts,
            note="수정 시도를 모두 소진했습니다.",
        )

    def _determinism_note(self, problem: Problem) -> str:
        """같은 입력을 두 번 돌려 출력이 같은지 본다 (PRD F2-B7)."""
        if not problem.examples:
            return ""
        sample = problem.examples[0].input.encode("utf-8")
        if check_determinism(self.executor, self.workspace.solution_path, sample, problem.limits):
            return ""
        self.bus.emit(
            STAGE_SOLUTION,
            "같은 입력에 대해 출력이 매번 달라집니다. 기대 출력을 신뢰할 수 없습니다.",
            LEVEL_WARN,
        )
        return "비결정적 출력이 감지되었습니다."

    def _apply_fix(self, problem: Problem, counterexample: Counterexample) -> bool:
        """반례를 LLM에 돌려주고 코드를 고친다 (PRD F2-B5)."""
        if counterexample.reason.startswith("브루트포스 실행 실패"):
            self.bus.emit(STAGE_SOLUTION, "브루트포스가 실패해 정답 후보를 자동 수정하지 않습니다.", LEVEL_ERROR)
            return False
        saved = self.workspace.reports_dir / "counterexamples.jsonl"
        with saved.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(counterexample.to_dict(), ensure_ascii=False) + "\n")
        self.bus.emit(STAGE_SOLUTION, "반례를 저장하고 코드를 수정하는 중입니다.")
        current = self.workspace.solution_path.read_text(encoding="utf-8")
        try:
            fixed = self.cli.ask_code(
                prompts.fix_solution(
                    problem,
                    current,
                    counterexample.input_text,
                    counterexample.solution_output,
                    counterexample.brute_output,
                ),
                tag="fix",
            )
            self._write_solution(fixed)
        except (LLMError, SafetyBlocked) as error:
            self.bus.emit(STAGE_SOLUTION, f"수정 실패: {error}", LEVEL_ERROR)
            return False
        return True

    def _stress_progress(self, done: int, total: int) -> None:
        self._check_cancel()
        if done % 50 == 0 or done == total:
            self.bus.emit(STAGE_SOLUTION, f"대조 진행 {done}/{total}")

    def _stress_generator_path(self) -> Path:
        return self.workspace.gens_dir / f"{STRESS_GENERATOR_NAME}.py"

    # --------------------------------------------------------------- 케이스

    def _invalidate(self, *keys: str) -> None:
        for key in keys:
            self._state.pop(key, None)
        if "cases" in keys or "examples" in keys:
            self._state.pop("export_metadata", None)
        if "export" in keys:
            problem_export.remove_archives(self.workspace)
        self._persist_state()

    def build_cases(self) -> generate.GenerationOutcome:
        """채택된 케이스를 기준으로 유형·생성기별 몫을 나누고 부족분을 재생성한다."""
        problem = self.problem
        if not problem.solution_confirmed:
            raise PipelineError("정답 코드가 확정되지 않아 케이스를 만들 수 없습니다.")
        self._invalidate("cases", "strength", "final", "export")
        self.workspace.clear_cases()
        target = max(MIN_CASE_COUNT, problem.case_plan.count)
        records = []
        rejections = []

        generated = reviewed = 0
        last_progress = 0.0

        def summary(*, force=False):
            nonlocal last_progress
            now = time.monotonic()
            if force or now - last_progress >= 0.5:
                last_progress = now
                self._progress(STAGE_CASES, f"케이스 {len(records)}/{target}개 확정 · 제외 {len(rejections)}개",
                    kind="case_progress", generated=generated, reviewed=reviewed,
                    accepted=len(records), target=target, rejected=len(rejections))

        def detail(origin, phase):
            nonlocal generated, reviewed
            if phase == "generate":
                generated += 1
            else:
                reviewed += 1
            self._progress(STAGE_CASES, f"{'생성' if phase == 'generate' else '심사'}: {origin}",
                           kind="case_detail", phase=phase, origin=origin)

        def accept(candidates):
            outcome = generate.materialize(
                self.executor, self.workspace, candidates, self.workspace.solution_path,
                problem.limits, validator_path=self.workspace.validator_path,
                start_index=len(records) + 1,
                existing_digests=generate.digests_of(self.workspace, records),
                on_progress=lambda origin: detail(origin, "review"),
            )
            records.extend(outcome.records)
            rejections.extend(outcome.rejections)
            summary()

        accept(generate.candidates_from_examples(problem.examples))
        self.bus.emit(STAGE_CASES, "경계·특수 케이스를 받아오는 중입니다.")
        payload = self.cli.ask_json(prompts.edge_cases(problem), tag="edge")
        edges = generate.candidates_from_payload(payload, label="edge", origin_prefix="edge")
        edge_quota = max(1, target // 4)
        for candidate in edges:
            if sum(record.label == "edge" for record in records) >= edge_quota:
                break
            if len(records) < target:
                accept([candidate])

        generators = self._prepare_generators(problem)
        remaining = max(0, target - len(records))
        quotas = {kind: remaining // 3 + (index < remaining % 3)
                  for index, kind in enumerate(_GENERATOR_KINDS)}
        used = {path: 0 for _, path in generators}
        # 채택에 실패한 생성기는 제한된 횟수만 재시도한다. 빈 입력 영역을 무한히 돌지 않는다.
        for wave in range(6):
            if len(records) >= target:
                break
            for kind in _GENERATOR_KINDS:
                paths = [path for label, path in generators if label == kind]
                deficit = quotas[kind] - sum(record.label == kind for record in records)
                # 마지막 두 회차에는 다른 유형의 부족분도 분산해 채운다.
                if wave >= 4:
                    deficit = max(deficit, (target - len(records) + 2) // 3)
                for offset in range(max(0, deficit)):
                    if not paths or len(records) >= target:
                        break
                    path = paths[offset % len(paths)]
                    used[path] += 1
                    produced, failed = generate.candidates_from_generator(
                        self.executor, path, label=kind, seeds=(used[path],),
                        max_bytes=problem.case_plan.max_input_bytes,
                        on_progress=lambda origin: detail(origin, "generate"),
                    )
                    rejections.extend(failed)
                    accept(produced)

        summary(force=True)
        merged = generate.GenerationOutcome(tuple(records), tuple(rejections))
        self.workspace.save_cases_index(records)
        self._state["cases"] = {**merged.to_dict(), "target_count": target,
                                "complete": len(records) >= target,
                                "distribution": {kind: sum(r.label == kind for r in records)
                                                 for kind in ("example", "edge", *_GENERATOR_KINDS)}}
        self._persist_state()
        complete = len(records) >= target
        self.bus.emit(STAGE_CASES,
                      f"케이스 {len(records)}/{target}건 확정 (탈락 {len(rejections)}건)"
                      + ("" if complete else " · 고유한 유효 입력 부족: 생성기를 보완해야 내보낼 수 있습니다."),
                      LEVEL_SUCCESS if complete else LEVEL_WARN, kind="case_complete")
        return merged

    def _prepare_generators(self, problem: Problem) -> list[tuple[str, Path]]:
        self.bus.emit(STAGE_CASES, "유형별 여러 생성기 스크립트를 준비합니다.")
        payload = self.cli.ask_json(prompts.case_generators(problem), tag="generators")
        generators = []
        for position, spec in enumerate((payload.get("generators") or [])[:12]):
            if not isinstance(spec, dict) or not isinstance(spec.get("source"), str):
                continue
            kind = str(spec.get("kind", ""))
            if kind not in _GENERATOR_KINDS or not spec["source"].strip():
                continue
            stem = _safe_stem(str(spec.get("name") or kind), position)
            path = self.workspace.gens_dir / f"{kind}_{position}_{stem}.py"
            try:
                self._warn_findings(STAGE_CASES, path.name, write_script(path, spec["source"]))
            except SafetyBlocked as error:
                self.bus.emit(STAGE_CASES, str(error), LEVEL_WARN)
                continue
            generators.append((kind, path))
        for kind in _GENERATOR_KINDS:
            if not any(label == kind for label, _ in generators):
                self.bus.emit(STAGE_CASES, f"{kind} 생성기가 없어 해당 유형을 생성하지 못합니다.", LEVEL_WARN)
        return generators

    # ----------------------------------------------------------------- 강도

    def verify_strength(
        self, *, reinforce_rounds: int = DEFAULT_REINFORCE_ROUNDS
    ) -> strength.StrengthReport:
        """오답 코드로 테스트 강도를 재고, 살아남으면 케이스를 보강한다 (PRD F2-D)."""
        problem = self.problem
        records = self.workspace.load_cases_index()
        if not records:
            raise PipelineError("테스트케이스가 없어 강도를 검증할 수 없습니다.")
        if not problem.solution_confirmed:
            raise PipelineError("정답 코드가 확정되지 않아 강도를 검증할 수 없습니다.")
        self._invalidate("strength", "final", "export")

        self.bus.emit(STAGE_STRENGTH, "오답 코드를 생성하는 중입니다.")
        payload = self.cli.ask_json(
            prompts.mutants(problem, self.workspace.solution_path.read_text(encoding="utf-8")),
            tag="mutants",
        )
        specs, paths = self._write_mutants(strength.specs_from_payload(payload))
        for retry in range(2):
            missing = {kind: max(0, required - sum(spec.kind == kind for spec in specs))
                       for kind, required in (("wa", 4), ("tle", 1), ("mle", 1))}
            if not any(missing.values()):
                break
            self.bus.emit(STAGE_STRENGTH, f"오답 코드 부족분을 추가 요청합니다: {missing}", LEVEL_WARN)
            payload = self.cli.ask_json(
                prompts.mutants(problem, self.workspace.solution_path.read_text(encoding="utf-8"))
                + f"\n추가로 필요한 개수: {missing}. 기존 이름과 겹치지 않게 작성: {[s.name for s in specs]}",
                tag=f"mutants-fill-{retry + 1}",
            )
            specs, paths = self._write_mutants((*specs, *strength.specs_from_payload(payload)))
        if sum(s.kind == "wa" for s in specs) < 4 or not all(any(s.kind == k for s in specs) for k in ("tle", "mle")):
            raise PipelineError("안전 검사·중복 제거 후 WA 4개 이상, TLE 1개, MLE 1개가 필요합니다. 오답 코드 생성을 다시 실행해 주세요.")

        report_result = self._evaluate(specs, paths, records, problem.limits)
        for round_index in range(1, min(3, max(0, reinforce_rounds)) + 1):
            if report_result.all_killed:
                break
            before_count, before_kills = len(records), sum(r.killed for r in report_result.results)
            records = self._reinforce(problem, report_result, records, round_index)
            report_result = self._evaluate(specs, paths, records, problem.limits)
            if len(records) == before_count or sum(r.killed for r in report_result.results) == before_kills:
                self.bus.emit(STAGE_STRENGTH, "보강 효과가 없어 종료합니다. 생존 오답은 리포트에 남깁니다.", LEVEL_WARN)
                break

        self._state["strength"] = report_result.to_dict()
        self._persist_state()
        level = LEVEL_SUCCESS if report_result.all_killed else LEVEL_WARN
        self.bus.emit(
            STAGE_STRENGTH,
            f"kill rate {report_result.kill_rate * 100:.0f}% "
            f"(생존 {len(report_result.survivors)}건)",
            level,
        )
        return report_result

    def _write_mutants(
        self, specs: Sequence[strength.MutantSpec]
    ) -> tuple[tuple[strength.MutantSpec, ...], tuple[Path, ...]]:
        kept: list[strength.MutantSpec] = []
        paths: list[Path] = []
        used: set[str] = set()
        sources: set[str] = set()
        for spec in specs:
            stem = f"{spec.kind}_{spec.name}"
            digest = hashlib.sha256(spec.source.strip().encode()).hexdigest()
            if stem in used or digest in sources:
                self.bus.emit(STAGE_STRENGTH, f"이름이 겹치는 오답 코드 제외: {stem}", LEVEL_WARN)
                continue
            used.add(stem)
            sources.add(digest)
            path = self.workspace.mutants_dir / f"{stem}.py"
            try:
                write_script(path, spec.source)
            except SafetyBlocked as error:
                self.bus.emit(STAGE_STRENGTH, f"오답 코드 제외: {error}", LEVEL_WARN)
                continue
            kept.append(spec)
            paths.append(path)
        return tuple(kept), tuple(paths)

    def _evaluate(
        self,
        specs: Sequence[strength.MutantSpec],
        paths: Sequence[Path],
        records: Sequence,
        limits: Limits,
    ) -> strength.StrengthReport:
        numbers = [record.index for record in records]
        return strength.evaluate_all(
            self.executor,
            self.workspace,
            specs,
            paths,
            numbers,
            limits,
            measurement=self.environment.measurement,
            isolated=self.environment.isolated,
            on_progress=lambda name: self._progress(STAGE_STRENGTH, f"검증: {name}"),
        )

    def _reinforce(
        self,
        problem: Problem,
        report_result: strength.StrengthReport,
        records: Sequence,
        round_index: int,
    ) -> tuple:
        """살아남은 오답을 잡는 케이스를 추가한다 (PRD F2-D5)."""
        self.bus.emit(
            STAGE_STRENGTH,
            f"[보강 {round_index}회차] 생존한 오답 {len(report_result.survivors)}건을 잡을 "
            "케이스를 추가로 만듭니다.",
            LEVEL_WARN,
        )
        candidates: list[generate.Candidate] = []
        for survivor in report_result.survivors:
            path = self.workspace.mutants_dir / f"{survivor.kind}_{survivor.name}.py"
            if not path.exists():
                continue
            try:
                payload = self.cli.ask_json(
                    prompts.anti_cases(
                        problem, path.read_text(encoding="utf-8"), survivor.flaw
                    ),
                    tag=f"anti-{survivor.name}",
                )
            except LLMError as error:
                self.bus.emit(STAGE_STRENGTH, f"보강 케이스 생성 실패: {error}", LEVEL_WARN)
                continue
            candidates.extend(
                generate.candidates_from_payload(
                    payload, label="anti", origin_prefix=f"anti:{survivor.name}"
                )
            )
            for position, spec in enumerate((payload.get("generators") or [])[:2]):
                if not isinstance(spec, dict) or not isinstance(spec.get("source"), str):
                    continue
                generator_path = self.workspace.gens_dir / f"reinforce_{survivor.name}_{round_index}_{position}.py"
                try:
                    write_script(generator_path, spec["source"])
                except SafetyBlocked as error:
                    self.bus.emit(STAGE_STRENGTH, str(error), LEVEL_WARN)
                    continue
                produced, _ = generate.candidates_from_generator(
                    self.executor, generator_path, label="anti", seeds=(1, 2, 3),
                    max_bytes=problem.case_plan.max_input_bytes,
                    on_progress=lambda origin: self._progress(STAGE_STRENGTH, f"보강 생성: {origin}"),
                )
                candidates.extend(produced)

        if not candidates:
            return tuple(records)

        outcome = generate.materialize(
            self.executor,
            self.workspace,
            candidates,
            self.workspace.solution_path,
            problem.limits,
            validator_path=self.workspace.validator_path,
            start_index=len(records) + 1,
            existing_digests=generate.digests_of(self.workspace, records),
            on_progress=lambda origin: self._progress(STAGE_STRENGTH, f"보강 심사: {origin}"),
        )
        merged = tuple(records) + outcome.records
        self.workspace.save_cases_index(merged)
        case_state = self._state.setdefault("cases", {})
        case_state.update(records=[record.to_dict() for record in merged], case_count=len(merged))
        case_state.setdefault("rejections", []).extend(rejection.to_dict() for rejection in outcome.rejections)
        self._persist_state()
        self.bus.emit(STAGE_STRENGTH, f"케이스 {len(outcome.records)}건을 보강했습니다.")
        return merged

    # ------------------------------------------------------------- 내보내기

    def export(self) -> exporter.ExportResult:
        """zip을 만들고 자체 검증한다 (PRD F3)."""
        problem = self.problem
        if not problem.is_exportable:
            raise PipelineError(
                "정답 코드가 확정되지 않아 내보낼 수 없습니다. 브루트포스 대조를 먼저 통과시키세요."
            )
        target = max(MIN_CASE_COUNT, problem.case_plan.count)
        if len(self.workspace.load_cases_index()) < target:
            raise PipelineError(f"중복 제거·검증 후 최소 {target}개 케이스가 필요합니다.")
        final = self.verify_final()
        if not final["passed"]:
            raise PipelineError("최종 대조가 실패해 내보내기를 중단했습니다. 리포트를 확인하세요.")
        export_metadata = self.prepare_export_metadata(final_checked=True)
        destination = problem_export.destination(self.workspace)
        self.bus.emit(STAGE_EXPORT, "문제 JSON·그림·테스트케이스를 zip으로 묶는 중입니다.")
        result = problem_export.export_problem(self.workspace, export_metadata)
        self._state["export"] = {**result.to_dict(), "format": problem_export.FORMAT}
        self._persist_state()
        if not result.has_errors:
            try:
                history.record(self.workspace, stage="export", status="exported", exported=True)
            except OSError as error:
                logger.warning("내보내기 이력을 저장하지 못했습니다: %s", error)

        for issue in result.issues:
            level = LEVEL_ERROR if issue.severity == exporter.SEVERITY_ERROR else LEVEL_WARN
            self.bus.emit(STAGE_EXPORT, issue.message, level)
        self.bus.emit(
            STAGE_EXPORT,
            f"내보내기 완료: {destination.name} ({result.case_count}건)",
            LEVEL_ERROR if result.has_errors else LEVEL_SUCCESS,
        )
        return result

    def prepare_export_metadata(self, *, final_checked: bool = False) -> dict:
        """메타데이터와 원문 예제를 검증해 저장한다. 동일 문제의 분류는 재사용한다."""
        if not self.problem.solution_confirmed:
            raise PipelineError("정답 코드를 먼저 확정해 주세요.")
        if not self.workspace.load_cases_index():
            raise PipelineError("최종 검증에 사용할 테스트케이스를 먼저 생성해 주세요.")
        samples = metadata.original_samples(self.problem)
        if not final_checked and not self.verify_final(invalidate_export=False)["passed"]:
            raise PipelineError("최종 검증에 실패해 문제 내보내기 정보를 생성할 수 없습니다.")
        self._check_cancel()
        source = self.workspace.solution_path.read_text(encoding="utf-8")
        progress = lambda message: self._progress(STAGE_EXPORT, message)
        cache_path = self.workspace.reports_dir / "classification.json"
        cached = self._state.get("export_metadata")
        if not cached:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
        classification = metadata.classify(self.problem, source, self.cli,
            cached=cached, on_progress=progress,
            cache_dir=self.workspace.root.parent / ".tcgen-classifications")
        # 과거 메타데이터에 남은 힌트 검토 결과도 새 내보내기에는 사용하지 않는다.
        classification = {key: value for key, value in classification.items() if key != "hint_review"}
        cache_path.write_text(json.dumps(classification, ensure_ascii=False, indent=2), encoding="utf-8")
        result = {**classification, **samples, "source": problem_export.SOURCE,
                  "statement_html": self._statement_html(cached)}
        # 그림 누락·필수 항목 오류도 실제 export 전에 확인한다.
        problem_export.build_payload(self.workspace, result)
        self._state["export_metadata"] = result
        self._persist_state()
        self.write_report()
        return result

    def _statement_html(self, cached: dict | None) -> dict | None:
        """지문에 코드 블록·표·수식 등 서식을 입힌다. 실패하면 평문으로 내보낸다."""
        texts = problem_export.section_texts(self.problem)
        previous = (cached or {}).get("statement_html")
        if isinstance(previous, dict) and previous.get("source_key") == statement_html.source_key(texts):
            return previous
        self._progress(STAGE_EXPORT, "지문에 서식(코드 블록·표·수식)을 입히는 중입니다.")
        try:
            return statement_html.format_sections(
                texts, self.cli, on_progress=lambda message: self._progress(STAGE_EXPORT, message))
        except Cancelled:
            raise
        except Exception as error:  # 서식은 선택 사항이라 어떤 실패도 내보내기를 막지 않는다
            self.bus.emit(STAGE_EXPORT, f"지문 서식 변환에 실패해 평문으로 내보냅니다: {error}", LEVEL_WARN)
            return None

    def verify_final(self, *, invalidate_export: bool = True) -> dict[str, Any]:
        if not self.problem.solution_confirmed:
            raise PipelineError("정답 코드가 확정되지 않아 최종 검증할 수 없습니다.")
        if invalidate_export:
            self._invalidate("final", "export")
        else:
            self._invalidate("final")
        result = finalize.verify_cases(
            self.executor, self.workspace,
            on_progress=lambda message: self._progress(STAGE_FINAL, message),
        )
        self._state["final"] = result
        if not result["passed"]:
            self._state.pop("export", None)
            problem_export.remove_archives(self.workspace)
        self._persist_state()
        self.bus.emit(STAGE_FINAL,
                      f"입력 검증 {result['inputs_validated']}건 · 정답 출력 재검증 {result['outputs_verified']}건 · "
                      f"최종 대조: 일치 {result['matched']}, 실패 {result['failed']}, 미검증 {result['skipped']}",
                      LEVEL_ERROR if not result["passed"] else (LEVEL_WARN if result["skipped"] else LEVEL_SUCCESS))
        self.write_report()
        return result

    def write_report(self) -> Path:
        """리포트를 기록한다 (PRD NF-R3)."""
        cases = self.workspace.load_cases_index()
        content = report.render_report(
            self.problem,
            cases=cases,
            strength=self._state.get("strength"),
            stress=self._state.get("stress"),
            final=self._state.get("final"),
            export_metadata=self._state.get("export_metadata"),
            export=self._state.get("export"),
            environment=self.environment.to_dict(),
            rejections=(self._state.get("cases") or {}).get("rejections", ()),
        )
        path = report.write_report(self.workspace.reports_dir, content)
        report.write_run_json(self.workspace.reports_dir, self._run_payload())
        self.bus.emit(STAGE_EXPORT, f"리포트를 기록했습니다: {path.name}", LEVEL_SUCCESS)
        return path

    def _run_payload(self) -> dict[str, Any]:
        return {
            "problem": self.problem.to_dict(),
            "environment": self.environment.to_dict(),
            "llm": {"provider": getattr(self.cli, "provider", "test"), "model": getattr(self.cli, "model", None)},
            "output_comparison": compare.comparison_for(self.problem).to_dict(),
            "examples": self._state.get("examples"),
            "stress": self._state.get("stress"),
            "cases": self._state.get("cases"),
            "strength": self._state.get("strength"),
            "final": self._state.get("final"),
            "export": self._state.get("export"),
            "export_metadata": self._state.get("export_metadata"),
        }

    def run_all(self, author_source: str | None = None, *, reuse_solution: bool = False) -> dict[str, Any]:
        """전자동 실행 (전문가 모드)."""
        try:
            reusable = (reuse_solution and self.problem.solution_confirmed and all(path.is_file() for path in (
                self.workspace.solution_path, self.workspace.brute_path,
                self.workspace.validator_path, self._stress_generator_path(),
            )))
            if not reusable:
                outcome = self.build_solution(author_source)
                if not outcome.confirmed:
                    raise PipelineError("정답 코드를 확정하지 못해 중단했습니다. 리포트를 확인하세요.")
            cases = self.build_cases()
            if len(cases.records) < max(MIN_CASE_COUNT, self.problem.case_plan.count):
                raise PipelineError("유효한 고유 케이스가 목표 수에 미달해 중단했습니다. 생성 결과를 확인하세요.")
            self.verify_strength()
            if self.export().has_errors:
                raise PipelineError("zip 검증에 실패했습니다. 내보내기 리포트를 확인하세요.")
        finally:
            # 중간에 실패해도 그때까지의 증거는 남겨야 한다 (PRD NF-R2, NF-R3).
            self.write_report()
        return self._run_payload()

    def _warn_findings(self, stage: str, label: str, findings: Sequence) -> None:
        for finding in findings:
            self.bus.emit(
                stage,
                f"{label} {finding.line}행 경고: {finding.detail}",
                LEVEL_WARN,
            )


def _seeds_for(position: int, remaining: int, generator_count: int) -> tuple[int, ...]:
    """생성기별로 시드를 나눠 준다. 시드는 재현을 위해 기록된다."""
    per_generator = max(1, remaining // max(1, generator_count))
    start = position * per_generator + 1
    return tuple(range(start, start + per_generator))


def _safe_stem(name: str, position: int) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_-" else "_" for char in name)
    return cleaned.strip("_").lower() or f"gen{position}"


def _as_int(value: Any, fallback: int) -> int:
    """LLM이 숫자가 아닌 값을 줘도 작업이 통째로 죽지 않게 한다."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
