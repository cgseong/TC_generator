"""첫 목록은 전체 집계·환경 점검·JavaScript 요청 전에 HTML에 있어야 한다."""

import json
import sqlite3
from html.parser import HTMLParser

import pytest

from test_boj import SCHEMA, _insert_problem
from test_server import client
from tcgen import boj, server


class CatalogHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.initial, self.ids, self.scripts = None, [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id") == "catalog-list":
            self.initial = attrs.get("data-initial-page")
        if "data-boj-id" in attrs:
            self.ids.append(attrs["data-boj-id"])
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])


@pytest.fixture
def collection(tmp_path, monkeypatch):
    path = tmp_path / "collection.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        for n in range(1000, 1012):
            _insert_problem(connection, n, title=f"문제 {n}")
    monkeypatch.setattr(boj, "BOJ_DB_PATH", path)
    return path


def test_initial_page_reads_only_limit_plus_one(collection, monkeypatch):
    connect, queries = boj.connect, []

    def spy(path=None):
        connection = connect(path)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(boj, "connect", spy)
    page = boj.initial_catalog_page()
    assert len(page["items"]) == 10
    assert page["has_more"]
    assert page["total"] is None
    assert any("LIMIT 11" in query for query in queries)
    assert not any("COUNT(" in query for query in queries)


def test_root_contains_first_ten_without_catalog_or_environment_work(client, collection, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("첫 목록이 전체 집계 또는 환경 점검을 기다렸습니다.")

    monkeypatch.setattr(boj, "_get_catalog", blocked)
    monkeypatch.setattr(server, "probe_environment", blocked)
    response = client.get("/")
    assert response.status_code == 200
    parser = CatalogHTML()
    parser.feed(response.text)
    assert parser.ids == [str(n) for n in range(1000, 1010)]
    assert len(json.loads(parser.initial)["items"]) == 10
    assert response.headers["cache-control"] == "no-store"
    assert parser.scripts[0].startswith("app.js?v=")
    assert "문제 목록을 불러오는 중입니다" not in response.text
    assert client.get("/index.html").status_code == 200


def test_bootstrap_titles_are_escaped(client, collection):
    with sqlite3.connect(collection) as connection:
        connection.execute("UPDATE problems SET title = ? WHERE problem_id=1000",
                           ('<img src=x onerror="alert(1)">',))
    response = client.get("/")
    assert '<img src=x onerror="alert(1)">' not in response.text
    assert "&lt;img src=x" in response.text
    parser = CatalogHTML()
    parser.feed(response.text)
    assert json.loads(parser.initial)["items"][0]["title"].startswith("<img")


def test_initial_page_with_fewer_than_ten_has_no_next_page(client, collection):
    with sqlite3.connect(collection) as connection:
        connection.execute("DELETE FROM problems WHERE problem_id > 1002")
    page = boj.initial_catalog_page()
    assert len(page["items"]) == 3
    assert not page["has_more"]
    response = client.get("/")
    assert 'id="catalog-next" class="secondary" disabled' in response.text


def test_missing_collection_shows_error_without_endless_loading(client, tmp_path, monkeypatch):
    monkeypatch.setattr(boj, "BOJ_DB_PATH", tmp_path / "missing.db")
    response = client.get("/")
    assert response.status_code == 200
    assert "백준 수집본을 찾지 못했습니다" in response.text
    assert "문제 목록을 불러오는 중입니다" not in response.text
