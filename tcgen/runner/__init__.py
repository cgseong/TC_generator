"""코드 실행기. 실행 환경을 어댑터로 추상화한다 (PRD 6.1).

``LocalRunner``가 1차 구현이며, Docker·WSL 실행기가 생기면 같은 프로토콜로
갈아끼운다.
"""

from tcgen.runner.base import RunResult, Runner, Verdict
from tcgen.runner.local import LocalRunner, python_command

__all__ = ["RunResult", "Runner", "Verdict", "LocalRunner", "python_command"]
