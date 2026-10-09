"""앱 전역 상수. 매직 넘버를 코드에 흩지 않기 위한 단일 출처."""

from __future__ import annotations

from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
WORKSPACES_DIR: Final[Path] = PROJECT_ROOT / "workspaces"

# 지문에 제한이 없을 때 가정하는 값 (PRD F1-5)
DEFAULT_TIME_LIMIT_MS: Final[int] = 2000
DEFAULT_MEMORY_LIMIT_MB: Final[int] = 256

# 실행 안전장치 (PRD NF-S2, NF-S3)
OUTPUT_LIMIT_BYTES: Final[int] = 16 * 1024 * 1024
MEMORY_POLL_INTERVAL_S: Final[float] = 0.01
# 자식 프로세스 목록 조회는 Windows에서 프로세스 테이블 전체를 훑으므로
# 매 폴링마다 하지 않는다. 측정 대상의 시간을 왜곡한다.
CHILD_REFRESH_INTERVAL_S: Final[float] = 0.1
KILL_GRACE_PERIOD_S: Final[float] = 0.5
# 시간제한을 넘겨도 바로 끊지 않고 이 배수까지 기다린 뒤 TLE로 확정한다.
TIME_LIMIT_SLACK: Final[float] = 2.0

# 테스트케이스 생성 기본값 (문제마다 UI에서 변경 가능, PRD F2-C6)
DEFAULT_CASE_COUNT: Final[int] = 25
DEFAULT_MAX_INPUT_BYTES: Final[int] = 4 * 1024 * 1024
DEFAULT_STRESS_ROUNDS: Final[int] = 300
MAX_STRESS_ROUNDS: Final[int] = 5000
# 대조를 0회 돌고 '통과'로 처리하면 정답 확정이 무의미해진다 (PRD F2-B4).
MIN_STRESS_ROUNDS: Final[int] = 1
DEFAULT_FIX_ATTEMPTS: Final[int] = 3
DEFAULT_REINFORCE_ROUNDS: Final[int] = 2

# LLM 호출
LLM_TIMEOUT_S: Final[float] = 600.0
LLM_MAX_RETRIES: Final[int] = 2

# 로컬 서버
SERVER_HOST: Final[str] = "127.0.0.1"
SERVER_PORT: Final[int] = 8765

MEASUREMENT_POLLING: Final[str] = "polling-lowres"
MEASUREMENT_UNAVAILABLE: Final[str] = "unavailable"
