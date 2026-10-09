"""실행 진입점.

``python -m tcgen`` 한 줄로 로컬 서버를 띄우고 브라우저를 연다.
``127.0.0.1``에만 바인딩하므로 외부에서 접근할 수 없다.
"""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from tcgen import server
from tcgen.config import SERVER_HOST, SERVER_PORT
from tcgen.runner.env import probe_environment

_BROWSER_DELAY_S = 1.0


def main() -> None:
    parser = argparse.ArgumentParser(description="TC_generator 로컬 서버")
    parser.add_argument("--host", default=SERVER_HOST, help="바인딩 주소 (기본 127.0.0.1)")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="포트")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않는다")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _print_environment()

    server.allow_hostname(args.host)
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"[경고] {args.host}에 바인딩합니다. 이 도구는 인증이 없으니 외부에 열지 마세요.")

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(_BROWSER_DELAY_S, webbrowser.open, args=(url,)).start()

    print(f"TC_generator: {url}")
    uvicorn.run("tcgen.server:app", host=args.host, port=args.port, log_level=args.log_level)


def _print_environment() -> None:
    """시작할 때 환경 상태를 그대로 알린다 (PRD NF-P2, NF-P3)."""
    report = probe_environment()
    print(f"[환경] {report.banner} · 측정 방식 {report.measurement}")
    missing = [tool.name for tool in report.tools if not tool.available]
    if missing:
        print(f"[환경] 설치되지 않은 도구: {', '.join(missing)}")
    if report.skipped_languages:
        print(f"[환경] SKIP되는 언어: {', '.join(report.skipped_languages)}")
    if not report.llm_available:
        print("[환경] claude CLI가 없어 LLM 단계를 쓸 수 없습니다.")


if __name__ == "__main__":
    main()
