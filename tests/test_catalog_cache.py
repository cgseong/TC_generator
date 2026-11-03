"""대형 지문 DB를 반복 스캔하지 않고 목록을 조회하는 캐시 회귀 검사."""

import sqlite3
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_boj import SCHEMA, _insert_problem
from tcgen import boj


@pytest.fixture
def catalog_db(tmp_path, monkeypatch):
    path = tmp_path / "problems.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        _insert_problem(connection, 1000, title="A+B")
        _insert_problem(connection, 1001, title="100%_문제")
    monkeypatch.setattr(boj, "CATALOG_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(boj, "_catalogs", OrderedDict())
    return path


def forbid_source(*args):
    raise AssertionError("메타데이터 캐시가 있는데 원본 DB를 다시 읽었습니다.")


def test_cached_pages_and_search_do_not_read_source(catalog_db, monkeypatch):
    first = boj.list_problems(db_path=catalog_db, limit=1)
    assert first["items"][0]["problem_id"] == 1000
    monkeypatch.setattr(boj, "connect", forbid_source)
    assert boj.list_problems(db_path=catalog_db, offset=1, limit=1)["items"][0]["problem_id"] == 1001
    assert boj.list_problems("a+b", db_path=catalog_db)["total"] == 1
    assert boj.list_problems("%_", db_path=catalog_db)["total"] == 1
    assert boj.list_problems("100", db_path=catalog_db)["total"] == 2


def test_disk_cache_works_after_process_memory_is_cleared(catalog_db, monkeypatch):
    expected = boj.list_problems(db_path=catalog_db)
    boj._catalogs.clear()
    monkeypatch.setattr(boj, "connect", forbid_source)
    assert boj.list_problems(db_path=catalog_db) == expected


def test_database_update_invalidates_memory_and_disk_cache(catalog_db):
    assert boj.list_problems(db_path=catalog_db)["total"] == 2
    with sqlite3.connect(catalog_db) as connection:
        _insert_problem(connection, 1002, title="새 문제")
    assert boj.list_problems(db_path=catalog_db)["total"] == 3
    boj._catalogs.clear()
    assert boj.list_problems("새 문제", db_path=catalog_db)["total"] == 1


def test_wal_only_update_invalidates_cache(catalog_db):
    connection = sqlite3.connect(catalog_db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        assert boj.list_problems(db_path=catalog_db)["total"] == 2
        before = catalog_db.stat()
        _insert_problem(connection, 1002, title="WAL에만 있는 문제")
        after = catalog_db.stat()
        assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        assert boj.list_problems("WAL", db_path=catalog_db)["total"] == 1
    finally:
        connection.close()


@pytest.mark.parametrize("content", ["broken JSON", '{"version":1,"rows":null}'])
def test_corrupt_disk_cache_is_rebuilt(catalog_db, content):
    expected = boj.list_problems(db_path=catalog_db)
    next(boj.CATALOG_CACHE_DIR.glob("*.json")).write_text(content)
    boj._catalogs.clear()
    assert boj.list_problems(db_path=catalog_db) == expected


def test_unwritable_cache_does_not_break_listing(catalog_db, tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    monkeypatch.setattr(boj, "CATALOG_CACHE_DIR", blocked / "cache")
    assert boj.list_problems(db_path=catalog_db)["total"] == 2


def test_missing_source_never_serves_stale_cached_data(catalog_db):
    boj.list_problems(db_path=catalog_db)
    catalog_db.unlink()
    with pytest.raises(boj.BojUnavailable):
        boj.list_problems(db_path=catalog_db)


def test_returned_pages_cannot_mutate_cached_rows(catalog_db):
    page = boj.list_problems(db_path=catalog_db)
    page["items"][0]["title"] = "modified"
    page["items"].clear()
    result = boj.list_problems(db_path=catalog_db)
    assert result["total"] == 2
    assert result["items"][0]["title"] == "A+B"


def test_concurrent_first_requests_read_source_once(catalog_db, monkeypatch):
    connect = boj.connect
    opened = []

    def spy(path):
        opened.append(path)
        return connect(path)

    monkeypatch.setattr(boj, "connect", spy)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: boj.list_problems(db_path=catalog_db), range(4)))
    assert all(result["total"] == 2 for result in results)
    assert len(opened) == 1
