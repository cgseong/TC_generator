"""단계별 오케스트레이션 (PRD 11장).

각 단계는 독립적으로 호출할 수 있다. 위저드 모드는 한 단계씩 부르고 사람이
확인하며, 전문가 모드는 :meth:`Pipeline.run_all`로 이어서 돌린다.

게이트:
- 정답 코드가 확정되기 전에는 케이스를 만들지 않는다 (F2-B6).
- 정답 코드가 확정되기 전에는 내보내지 않는다 (F3-7).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from tcgen import exporter
from tcgen.config import (
    DEFAULT_FIX_ATTEMPTS,
    DEFAULT_REINFORCE_ROUNDS,
    MAX_STRESS_ROUNDS,
)
from tcgen.engine import generate, prompts, report, strength
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
from tcgen.llm import ClaudeCLI, LLMError
from tcgen.models import Example, Figure, Limits, Problem
from tcgen.runner.base import Runner
from tcgen.runner.env import EnvironmentReport, probe_environment
from tcgen.runner.local import LocalRunner
from tcgen.workspace import Workspace

logger = logging.getLogger(__name__)

STAGE_PARSE = "parse"
STAGE_INTERPRET = "interpret"
STAGE_SOLUTION = "solution"
STAGE_CASES = "cases"
STAGE_STRENGTH = "strength"
STAGE_EXPORT = "export"

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
        cli: ClaudeCLI,
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

    def _progress(self, stage: str, message: str) -> None:
        self._check_cancel()
        self.bus.emit(stage, message)

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
        return self._save(updated)

    def parse_pdf(self) -> Problem:
        """PDF 지문을 읽어 구조화 항목과 그림 전사를 채운다 (PRD F1-7).

        그림이 들어간 문제의 유일한 입력 경로다. 텍스트 붙여넣기와 달리 CLI가
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
        return self._save(updated)

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

    def interpret(self) -> dict[str, Any]:
        """해석 요약과 모호한 지점을 뽑는다 (PRD F1-4)."""
        self.bus.emit(STAGE_INTERPRET, "지문 해석을 정리하는 중입니다.")
        payload = self.cli.ask_json(prompts.interpretation(self.problem), tag="interpret")
        summary = str(payload.get("summary", ""))
        self._save(self.problem.with_changes(interpretation=summary))
        ambiguities = [str(item) for item in payload.get("ambiguities", []) or []]
        if ambiguities:
            self.bus.emit(
                STAGE_INTERPRET,
                f"확인이 필요한 지점 {len(ambiguities)}건을 찾았습니다.",
                LEVEL_WARN,
                ambiguities=ambiguities,
            )
        self._state["interpretation"] = payload
        return payload

    def confirm_interpretation(self) -> Problem:
        """사람이 해석을 확인했음을 기록한다."""
        self.bus.emit(STAGE_INTERPRET, "지문 해석을 확인했습니다.", LEVEL_SUCCESS)
        return self._save(self.problem.with_changes(interpretation_confirmed=True))

    # ----------------------------------------------------------------- 정답

    def build_solution(
        self,
        author_source: str | None = None,
        *,
        max_attempts: int = DEFAULT_FIX_ATTEMPTS,
    ) -> SolutionOutcome:
        """정답 코드를 확보하고 브루트포스 대조로 확정한다 (PRD F2-B)."""
        problem = self.problem
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
            )
            if stress.passed:
                return SolutionOutcome(
                    True, examples, stress, attempt, note=self._determinism_note(problem)
                )

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
        self.bus.emit(STAGE_SOLUTION, "반례를 바탕으로 코드를 수정하는 중입니다.")
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

    def build_cases(self) -> generate.GenerationOutcome:
        """엣지 명시 + 생성기 실행으로 케이스를 만든다 (PRD F2-C)."""
        problem = self.problem
        if not problem.solution_confirmed:
            raise PipelineError("정답 코드가 확정되지 않아 케이스를 만들 수 없습니다.")

        self.workspace.clear_cases()
        self.bus.emit(STAGE_CASES, "경계 케이스를 받아오는 중입니다.")
        edge_payload = self.cli.ask_json(prompts.edge_cases(problem), tag="edge")
        candidates = [
            *generate.candidates_from_examples(problem.examples),
            *generate.candidates_from_payload(edge_payload, label="edge", origin_prefix="edge"),
        ]

        generated, rejections = self._run_generators(problem, len(candidates))
        candidates.extend(generated)

        self.bus.emit(STAGE_CASES, f"후보 {len(candidates)}건을 심사하는 중입니다.")
        outcome = generate.materialize(
            self.executor,
            self.workspace,
            candidates,
            self.workspace.solution_path,
            problem.limits,
            validator_path=self.workspace.validator_path,
            on_progress=lambda origin: self._progress(STAGE_CASES, f"심사: {origin}"),
        )
        merged = generate.GenerationOutcome(
            records=outcome.records, rejections=rejections + outcome.rejections
        )
        self.workspace.save_cases_index(merged.records)
        self._state["cases"] = merged.to_dict()
        self._persist_state()
        self.bus.emit(
            STAGE_CASES,
            f"케이스 {len(merged.records)}건 확정 (탈락 {len(merged.rejections)}건)",
            LEVEL_SUCCESS,
        )
        return merged

    def _run_generators(
        self, problem: Problem, already: int
    ) -> tuple[list[generate.Candidate], tuple[generate.Rejection, ...]]:
        self.bus.emit(STAGE_CASES, "생성기 스크립트를 받아오는 중입니다.")
        payload = self.cli.ask_json(prompts.case_generators(problem), tag="generators")
        remaining = max(len(_GENERATOR_KINDS), problem.case_plan.count - already)
        candidates: list[generate.Candidate] = []
        rejections: list[generate.Rejection] = []

        for position, spec in enumerate(payload.get("generators", []) or []):
            if not isinstance(spec, dict) or not str(spec.get("source", "")).strip():
                continue
            kind = str(spec.get("kind") or spec.get("name") or f"gen{position}")
            path = self.workspace.gens_dir / f"{_safe_stem(kind, position)}.py"
            try:
                self._warn_findings(
                    STAGE_CASES, path.name, write_script(path, str(spec["source"]))
                )
            except SafetyBlocked as error:
                rejections.append(generate.Rejection(path.name, str(error)))
                continue
            seeds = _seeds_for(position, remaining, len(_GENERATOR_KINDS))
            produced, failed = generate.candidates_from_generator(
                self.executor,
                path,
                label=kind,
                seeds=seeds,
                max_bytes=problem.case_plan.max_input_bytes,
                on_progress=lambda origin: self._progress(STAGE_CASES, f"생성: {origin}"),
            )
            candidates.extend(produced)
            rejections.extend(failed)
        return candidates, tuple(rejections)

    # ----------------------------------------------------------------- 강도

    def verify_strength(
        self, *, reinforce_rounds: int = DEFAULT_REINFORCE_ROUNDS
    ) -> strength.StrengthReport:
        """오답 코드로 테스트 강도를 재고, 살아남으면 케이스를 보강한다 (PRD F2-D)."""
        problem = self.problem
        records = self.workspace.load_cases_index()
        if not records:
            raise PipelineError("테스트케이스가 없어 강도를 검증할 수 없습니다.")

        self.bus.emit(STAGE_STRENGTH, "오답 코드를 생성하는 중입니다.")
        payload = self.cli.ask_json(
            prompts.mutants(problem, self.workspace.solution_path.read_text(encoding="utf-8")),
            tag="mutants",
        )
        specs, paths = self._write_mutants(strength.specs_from_payload(payload))
        if not specs:
            raise PipelineError("쓸 수 있는 오답 코드를 받지 못했습니다.")

        report_result = self._evaluate(specs, paths, records, problem.limits)
        for round_index in range(1, reinforce_rounds + 1):
            if report_result.all_killed:
                break
            records = self._reinforce(problem, report_result, records, round_index)
            report_result = self._evaluate(specs, paths, records, problem.limits)

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
        for spec in specs:
            stem = f"{spec.kind}_{spec.name}"
            if stem in used:
                self.bus.emit(STAGE_STRENGTH, f"이름이 겹치는 오답 코드 제외: {stem}", LEVEL_WARN)
                continue
            used.add(stem)
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
        destination = self.workspace.export_dir / f"{problem.slug}_testcases.zip"
        self.bus.emit(STAGE_EXPORT, "zip을 만드는 중입니다.")
        result = exporter.export_zip(self.workspace.cases_dir, destination)
        self._state["export"] = result.to_dict()
        self._persist_state()

        for issue in result.issues:
            level = LEVEL_ERROR if issue.severity == exporter.SEVERITY_ERROR else LEVEL_WARN
            self.bus.emit(STAGE_EXPORT, issue.message, level)
        self.bus.emit(
            STAGE_EXPORT,
            f"내보내기 완료: {destination.name} ({result.case_count}건)",
            LEVEL_ERROR if result.has_errors else LEVEL_SUCCESS,
        )
        return result

    def write_report(self) -> Path:
        """리포트를 기록한다 (PRD NF-R3)."""
        cases = self.workspace.load_cases_index()
        content = report.render_report(
            self.problem,
            cases=cases,
            strength=self._state.get("strength"),
            stress=self._state.get("stress"),
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
            "examples": self._state.get("examples"),
            "stress": self._state.get("stress"),
            "cases": self._state.get("cases"),
            "strength": self._state.get("strength"),
            "export": self._state.get("export"),
        }

    def run_all(self) -> dict[str, Any]:
        """전자동 실행 (전문가 모드)."""
        if not self.problem.interpretation_confirmed:
            self.bus.emit(
                STAGE_INTERPRET,
                "지문 해석을 사람이 확인하지 않은 채 진행합니다. 리포트에 '미확인'으로 남습니다.",
                LEVEL_WARN,
            )
        try:
            outcome = self.build_solution()
            if not outcome.confirmed:
                raise PipelineError("정답 코드를 확정하지 못해 중단했습니다. 리포트를 확인하세요.")
            self.build_cases()
            self.verify_strength()
            self.export()
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
