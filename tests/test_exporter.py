"""QingdaoU 패키징 테스트 (PRD AC-7, F3).

이 테스트가 깨지면 OJ 업로드가 조용히 실패한다.
"""

import hashlib
import json
import zipfile

import pytest

from tcgen import exporter
from tcgen.exporter import ExportError


def make_cases(cases_dir, count: int = 2):
    cases_dir.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        (cases_dir / f"{number}.in").write_bytes(f"{number} {number}\n".encode())
        (cases_dir / f"{number}.out").write_bytes(f"{number * 2}".encode())
    return cases_dir


class TestWriteCase:
    def test_output_is_stored_with_lf_only(self, tmp_path):
        record = exporter.write_case(tmp_path, 1, b"1 2\n", b"3\r\n", label="example")
        assert (tmp_path / "1.out").read_bytes() == b"3"
        assert record.crlf_normalized is True

    def test_input_crlf_is_normalized(self, tmp_path):
        exporter.write_case(tmp_path, 1, b"1 2\r\n", b"3\n", label="example")
        assert (tmp_path / "1.in").read_bytes() == b"1 2\n"

    def test_record_sizes_match_written_files(self, tmp_path):
        record = exporter.write_case(tmp_path, 1, b"1 2\n", b"3\n", label="edge", seed=7)
        assert record.input_bytes == len((tmp_path / "1.in").read_bytes())
        assert record.output_bytes == len((tmp_path / "1.out").read_bytes())
        assert record.seed == 7


class TestInspectCases:
    def test_empty_directory_is_error(self, tmp_path):
        issues = exporter.inspect_cases(tmp_path / "cases")
        assert issues[0].severity == "error"

    def test_gap_in_numbering_is_error(self, tmp_path):
        cases = make_cases(tmp_path / "cases", count=1)
        (cases / "3.in").write_bytes(b"3 3\n")
        (cases / "3.out").write_bytes(b"6")
        issues = exporter.inspect_cases(cases)
        assert any("연속이 아닙니다" in issue.message for issue in issues)

    def test_missing_output_file_is_error(self, tmp_path):
        cases = make_cases(tmp_path / "cases", count=1)
        (cases / "1.out").unlink()
        issues = exporter.inspect_cases(cases)
        assert any(issue.severity == "error" for issue in issues)

    def test_crlf_output_is_error(self, tmp_path):
        cases = make_cases(tmp_path / "cases", count=1)
        (cases / "1.out").write_bytes(b"1\r\n2")
        issues = exporter.inspect_cases(cases)
        assert any("CRLF" in issue.message for issue in issues)

    def test_interior_trailing_space_is_warning(self, tmp_path):
        cases = make_cases(tmp_path / "cases", count=1)
        (cases / "1.out").write_bytes(b"1 \n2")
        issues = exporter.inspect_cases(cases)
        assert any(issue.severity == "warning" and "공백" in issue.message for issue in issues)

    def test_clean_cases_have_no_errors(self, tmp_path):
        issues = exporter.inspect_cases(make_cases(tmp_path / "cases"))
        assert not [issue for issue in issues if issue.severity == "error"]


class TestBuildInfo:
    def test_structure_matches_server_schema(self, tmp_path):
        cases = make_cases(tmp_path / "cases")
        info = exporter.build_info(cases, (1, 2))

        assert info["spj"] is False
        assert set(info["test_cases"]) == {"1", "2"}
        assert set(info["test_cases"]["1"]) == {
            "stripped_output_md5",
            "input_size",
            "output_size",
            "input_name",
            "output_name",
        }

    def test_md5_matches_rstrip_rule(self, tmp_path):
        cases = tmp_path / "cases"
        cases.mkdir()
        (cases / "1.in").write_bytes(b"x\n")
        (cases / "1.out").write_bytes(b"42\n\n")
        info = exporter.build_info(cases, (1,))
        assert info["test_cases"]["1"]["stripped_output_md5"] == hashlib.md5(b"42").hexdigest()

    def test_no_output_md5_key(self, tmp_path):
        info = exporter.build_info(make_cases(tmp_path / "cases"), (1, 2))
        assert "output_md5" not in info["test_cases"]["1"]


class TestExportZip:
    def test_zip_contains_exactly_expected_files(self, tmp_path):
        cases = make_cases(tmp_path / "cases")
        result = exporter.export_zip(cases, tmp_path / "export" / "p.zip")

        with zipfile.ZipFile(result.zip_path) as archive:
            assert sorted(archive.namelist()) == ["1.in", "1.out", "2.in", "2.out", "info"]

    def test_metadata_file_is_named_info_without_extension(self, tmp_path):
        cases = make_cases(tmp_path / "cases")
        result = exporter.export_zip(cases, tmp_path / "p.zip")
        with zipfile.ZipFile(result.zip_path) as archive:
            assert "info.json" not in archive.namelist()
            assert "info" in archive.namelist()

    def test_info_is_indented_by_four(self, tmp_path):
        cases = make_cases(tmp_path / "cases")
        result = exporter.export_zip(cases, tmp_path / "p.zip")
        with zipfile.ZipFile(result.zip_path) as archive:
            text = archive.read("info").decode("utf-8")
        assert '\n    "spj"' in text

    def test_files_are_at_zip_root(self, tmp_path):
        cases = make_cases(tmp_path / "cases")
        result = exporter.export_zip(cases, tmp_path / "p.zip")
        with zipfile.ZipFile(result.zip_path) as archive:
            assert all("/" not in name for name in archive.namelist())

    def test_export_result_reports_case_count(self, tmp_path):
        cases = make_cases(tmp_path / "cases", count=3)
        result = exporter.export_zip(cases, tmp_path / "p.zip")
        assert result.case_count == 3
        assert not result.has_errors

    def test_gap_blocks_export(self, tmp_path):
        cases = make_cases(tmp_path / "cases", count=1)
        (cases / "5.in").write_bytes(b"x\n")
        (cases / "5.out").write_bytes(b"y")
        with pytest.raises(ExportError):
            exporter.export_zip(cases, tmp_path / "p.zip")

    def test_empty_cases_block_export(self, tmp_path):
        with pytest.raises(ExportError):
            exporter.export_zip(tmp_path / "cases", tmp_path / "p.zip")


class TestVerifyZip:
    def test_clean_zip_passes(self, tmp_path):
        cases = make_cases(tmp_path / "cases")
        result = exporter.export_zip(cases, tmp_path / "p.zip")
        assert exporter.verify_zip(result.zip_path) == ()

    def test_tampered_md5_is_detected(self, tmp_path):
        cases = make_cases(tmp_path / "cases", count=1)
        broken = tmp_path / "broken.zip"
        with zipfile.ZipFile(broken, "w") as archive:
            archive.write(cases / "1.in", "1.in")
            archive.write(cases / "1.out", "1.out")
            archive.writestr(
                "info",
                json.dumps(
                    {
                        "spj": False,
                        "test_cases": {
                            "1": {
                                "stripped_output_md5": "deadbeef",
                                "input_size": 4,
                                "output_size": 1,
                                "input_name": "1.in",
                                "output_name": "1.out",
                            }
                        },
                    },
                    indent=4,
                ),
            )
        issues = exporter.verify_zip(broken)
        assert any("stripped_output_md5" in issue.message for issue in issues)

    def test_missing_info_is_detected(self, tmp_path):
        broken = tmp_path / "noinfo.zip"
        with zipfile.ZipFile(broken, "w") as archive:
            archive.writestr("1.in", "1 1\n")
            archive.writestr("1.out", "2")
        issues = exporter.verify_zip(broken)
        assert any("info" in issue.message for issue in issues)

    def test_nested_folder_is_detected(self, tmp_path):
        cases = make_cases(tmp_path / "cases", count=1)
        nested = tmp_path / "nested.zip"
        info = exporter.build_info(cases, (1,))
        with zipfile.ZipFile(nested, "w") as archive:
            archive.write(cases / "1.in", "1.in")
            archive.write(cases / "1.out", "1.out")
            archive.writestr("sub/extra.txt", "x")
            archive.writestr("info", json.dumps(info, indent=4))
        issues = exporter.verify_zip(nested)
        assert any("하위 폴더" in issue.message for issue in issues)

    def test_broken_file_is_reported(self, tmp_path):
        broken = tmp_path / "not-a-zip.zip"
        broken.write_bytes(b"definitely not a zip")
        issues = exporter.verify_zip(broken)
        assert issues and issues[0].severity == "error"
