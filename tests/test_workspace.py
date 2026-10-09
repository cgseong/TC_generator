"""작업공간 폴더 구조·직렬화 테스트 (PRD AC-9)."""

import pytest

from tcgen.models import CaseRecord, Example, Limits, Problem
from tcgen.workspace import (
    SUBDIRECTORIES,
    WorkspaceError,
    Workspace,
    create_workspace,
    list_workspaces,
    open_workspace,
    slugify,
)


def make_problem(title: str = "두 수의 합") -> Problem:
    return Problem(
        slug=slugify(title),
        title=title,
        statement="두 정수를 더한다.",
        input_spec="첫 줄에 A와 B",
        output_spec="A+B",
        constraints="0 <= A, B <= 10",
        examples=(Example(input="1 2\n", output="3\n"),),
        limits=Limits(time_ms=1000, memory_mb=128),
    )


class TestSlugify:
    def test_keeps_korean(self):
        assert slugify("두 수의 합") == "두-수의-합"

    def test_keeps_plus_sign(self):
        assert slugify("A+B 문제!") == "a+b-문제"

    def test_collapses_repeated_separators(self):
        assert slugify("a   ///   b") == "a-b"

    def test_empty_falls_back(self):
        assert slugify("  ///  ") == "problem"

    def test_truncates_long_titles(self):
        assert len(slugify("가" * 200)) <= 60


class TestCreateWorkspace:
    def test_creates_all_subdirectories(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        for name in SUBDIRECTORIES:
            assert (workspace.root / name).is_dir()

    def test_writes_problem_json(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        assert workspace.problem_path.exists()

    def test_duplicate_title_does_not_overwrite(self, tmp_path):
        first = create_workspace(make_problem(), base_dir=tmp_path)
        second = create_workspace(make_problem(), base_dir=tmp_path)
        assert first.root != second.root
        assert second.slug.endswith("-2")

    def test_slug_is_stored_in_problem(self, tmp_path):
        create_workspace(make_problem(), base_dir=tmp_path)
        second = create_workspace(make_problem(), base_dir=tmp_path)
        assert second.load_problem().slug == second.slug


class TestRoundTrip:
    def test_problem_survives_save_and_load(self, tmp_path):
        problem = make_problem()
        workspace = create_workspace(problem, base_dir=tmp_path)
        loaded = workspace.load_problem()
        assert loaded.title == problem.title
        assert loaded.examples == problem.examples
        assert loaded.limits == problem.limits

    def test_cases_index_survives_save_and_load(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        records = (
            CaseRecord(index=1, label="example", origin="example#1", input_bytes=4),
            CaseRecord(index=2, label="random", seed=42, input_bytes=9),
        )
        workspace.save_cases_index(records)
        assert workspace.load_cases_index() == records

    def test_missing_cases_index_returns_empty(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        assert workspace.load_cases_index() == ()


class TestOpenAndList:
    def test_open_missing_workspace_raises(self, tmp_path):
        with pytest.raises(WorkspaceError):
            open_workspace("없는문제", base_dir=tmp_path)

    def test_list_ignores_folders_without_problem_json(self, tmp_path):
        create_workspace(make_problem(), base_dir=tmp_path)
        (tmp_path / "쓰레기폴더").mkdir()
        assert len(list_workspaces(base_dir=tmp_path)) == 1

    def test_list_on_missing_base_dir_is_empty(self, tmp_path):
        assert list_workspaces(base_dir=tmp_path / "없음") == ()


class TestContains:
    def test_inside_path_is_contained(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        assert workspace.contains(workspace.cases_dir / "1.in")

    def test_outside_path_is_rejected(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        assert not workspace.contains(tmp_path / "다른곳" / "1.in")


class TestClearCases:
    def test_removes_existing_case_files(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        workspace.input_path(1).write_bytes(b"1 2\n")
        workspace.clear_cases()
        assert not workspace.input_path(1).exists()
        assert workspace.cases_dir.is_dir()


class TestExportGate:
    def test_unconfirmed_solution_blocks_export(self):
        assert not make_problem().is_exportable

    def test_confirmed_solution_allows_export(self):
        assert make_problem().with_changes(solution_confirmed=True).is_exportable
