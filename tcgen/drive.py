"""공개 Drive 폴더 또는 공식 API에서 선택한 문제의 그림만 받는다."""

from __future__ import annotations

import os
import re
import time
from html.parser import HTMLParser
from threading import Lock
from urllib.parse import urlparse

import httpx

from tcgen import boj, images
from tcgen.workspace import Workspace

_ID = re.compile(r"[A-Za-z0-9_-]{10,}")
_FOLDER = "application/vnd.google-apps.folder"
_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = Lock()


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.current, self.parts = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        parsed = urlparse(href)
        if parsed.hostname != "drive.google.com":
            return
        match = re.fullmatch(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,})/?", parsed.path)
        if match:
            self.current = {"id": match.group(1), "mimeType": _FOLDER}
        else:
            match = re.fullmatch(r"/file/d/([A-Za-z0-9_-]{10,})/view", parsed.path)
            if match:
                self.current = {"id": match.group(1), "mimeType": "image"}
        self.parts = []

    def handle_data(self, data):
        if self.current is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.items.append({**self.current, "name": "".join(self.parts).strip()})
            self.current, self.parts = None, []


def _read_public(client, url: str, *, params: dict, limit: int) -> bytes:
    for _ in range(5):
        with client.stream("GET", url, params=params) as response:
            if response.is_redirect:
                target = urlparse(response.headers.get("location", ""))
                if target.scheme != "https" or target.hostname not in {
                    "drive.google.com", "drive.usercontent.google.com", "docs.googleusercontent.com"}:
                    raise DriveError("공개 공유 링크로 접근하지 못했습니다. 링크가 있는 모든 사용자에게 보기 권한이 필요합니다.")
                url, params = target.geturl(), {}
                continue
            if response.status_code != 200:
                raise DriveError(f"공개 Drive 요청 실패({response.status_code}). 공유 권한을 확인하세요.")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise DriveError("Drive 응답이 허용된 크기를 넘었습니다.")
            return bytes(data)
    raise DriveError("Drive 리다이렉트가 너무 많습니다.")


def _public_listing(client, parent: str) -> list[dict]:
    with _cache_lock:
        cached = _cache.get(parent)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    data = _read_public(client, "https://drive.google.com/embeddedfolderview",
                        params={"id": parent}, limit=12 * 1024 * 1024)
    parser = _Links()
    parser.feed(data.decode("utf-8"))
    if not parser.items:
        raise DriveError("공개 폴더 목록을 읽지 못했습니다. 공유 권한을 확인하거나 수동으로 첨부해 주세요.")
    with _cache_lock:
        if len(_cache) >= 128:
            _cache.clear()
        _cache[parent] = (time.monotonic(), parser.items)
    return parser.items


class DriveError(ValueError):
    pass


def folder_id(value: str) -> str:
    if _ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "drive.google.com":
        raise DriveError("구글 드라이브 공유 폴더 링크를 입력해 주세요.")
    match = re.fullmatch(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,})/?", parsed.path)
    if not match:
        raise DriveError("파일 링크 대신 공유 폴더 링크를 입력해 주세요.")
    return match.group(1)


def credentials_available() -> bool:
    return bool(os.environ.get("TCGEN_DRIVE_API_KEY") or os.environ.get("TCGEN_DRIVE_ACCESS_TOKEN"))


def fetch_figures(workspace: Workspace, folder: str, *, client: httpx.Client | None = None) -> dict:
    """루트/[images/]문제번호/1.avif 구조. 인증정보는 환경변수에서만 읽는다."""
    root = folder_id(folder)
    problem = workspace.load_problem()
    if problem.boj_id is None:
        raise DriveError("백준에서 가져온 문제를 먼저 선택하세요.")
    if not problem.figures:
        return {"downloaded": 0, "cached": 0}
    owned = client is None
    if client is None:
        client = httpx.Client(timeout=30, follow_redirects=False)
    headers = {}
    token = os.environ.get("TCGEN_DRIVE_ACCESS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    key = os.environ.get("TCGEN_DRIVE_API_KEY")
    if key:
        headers["X-Goog-Api-Key"] = key

    def check(response):
        if response.status_code != 200:
            # 인증 실패 응답의 원문을 브라우저나 리포트에 남기지 않는다.
            raise DriveError(f"Drive 요청 실패({response.status_code}). 공유 권한·인증·API 활성화를 확인하세요.")

    def listing(parent, extra=""):
        if not credentials_available():
            found = _public_listing(client, parent)
            if "mimeType =" in extra:
                found = [item for item in found if item["mimeType"] == _FOLDER]
            match = re.search(r"name = '([^']+)'", extra)
            return [item for item in found if item["name"] == match.group(1)] if match else found
        found, page = [], None
        for _ in range(100):
            query = f"'{parent}' in parents and trashed = false" + extra
            response = client.get("https://www.googleapis.com/drive/v3/files", headers=headers,
                                  params=dict(q=query, fields="nextPageToken,incompleteSearch,files(id,name,mimeType,size)",
                                              pageSize=1000, **({"pageToken": page} if page else {})))
            check(response)
            payload = response.json()
            if payload.get("incompleteSearch"):
                raise DriveError("Drive 검색이 불완전합니다. 문제번호 폴더의 상위 폴더를 지정해 주세요.")
            found.extend(payload.get("files", []))
            page = payload.get("nextPageToken")
            if not page:
                return found
        raise DriveError("Drive 폴더 목록이 너무 큽니다.")

    try:
        number = str(problem.boj_id)
        extra = f" and mimeType = '{_FOLDER}' and name = '{number}'"
        matches = listing(root, extra)
        if not matches:
            image_roots = listing(root, f" and mimeType = '{_FOLDER}' and name = 'images'")
            if len(image_roots) == 1:
                matches = listing(image_roots[0]["id"], extra)
        if len(matches) != 1:
            raise DriveError(f"{number} 폴더를 하나로 특정하지 못했습니다. 루트/문제번호 또는 루트/images/문제번호 구조를 확인하세요.")
        files = listing(matches[0]["id"])
        numbered = []
        for item in files:
            match = boj._IMAGE_NAME.fullmatch(item.get("name", ""))
            if match:
                numbered.append((int(match.group(1)), item))
        numbered.sort(key=lambda pair: pair[0])
        if [n for n, _ in numbered] != list(range(1, len(problem.figures) + 1)):
            raise DriveError("지문의 그림 수·번호와 Drive 파일의 1..N 번호가 다릅니다. 자동 연결을 중단했습니다.")
        staged, cached = [], 0
        for n, item in numbered:
            if images.find(workspace, n):
                cached += 1
                continue
            if not _ID.fullmatch(str(item.get("id", ""))):
                raise DriveError("Drive 파일 ID가 올바르지 않습니다.")
            if int(item.get("size", 0)) > boj.MAX_FIGURE_BYTES:
                raise DriveError(f"{n}번 그림이 16MB를 넘습니다.")
            if not credentials_available():
                data = _read_public(client, "https://drive.usercontent.google.com/download",
                                    params={"id": item["id"], "export": "download", "confirm": "t"},
                                    limit=boj.MAX_FIGURE_BYTES)
                staged.append((n, images.normalize(data)))
                continue
            chunks, total = [], 0
            with client.stream("GET", f"https://www.googleapis.com/drive/v3/files/{item['id']}",
                               headers=headers, params={"alt": "media", "supportsAllDrives": "true"}) as response:
                check(response)
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > boj.MAX_FIGURE_BYTES:
                        raise DriveError(f"{n}번 그림이 16MB를 넘습니다.")
                    chunks.append(chunk)
            staged.append((n, images.normalize(b"".join(chunks))))
        for n, data in staged:
            images.store(workspace, n, data)
        if staged:
            workspace.save_problem(problem.with_changes(solution_confirmed=False))
        return {"downloaded": len(staged), "cached": cached}
    except (httpx.HTTPError, KeyError, TypeError) as error:
        raise DriveError("Drive 응답을 받거나 읽지 못했습니다. 네트워크·공유 권한을 확인하세요.") from error
    finally:
        if owned:
            client.close()
