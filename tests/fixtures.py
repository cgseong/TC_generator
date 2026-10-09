"""통합 테스트용 가짜 LLM과 A+B 문제 자산.

실제 CLI를 부르지 않고, 실행기·비교기·패키징은 진짜로 돌린다.
"""

from __future__ import annotations

import json
from typing import Any

from tcgen.models import CasePlan, Example, Limits, Problem
from tcgen.workspace import slugify

SOLUTION = """
import sys

a, b = map(int, sys.stdin.read().split())
print(a + b)
""".strip()

WRONG_SOLUTION = """
import sys

a, b = map(int, sys.stdin.read().split())
print(a * b)
""".strip()

#: 예제(1 2)는 통과하지만 A>5인 입력에서 틀린다. 스트레스 대조만이 잡을 수 있다.
STRESS_ONLY_WRONG_SOLUTION = """
import sys

a, b = map(int, sys.stdin.read().split())
print(a + b + (1 if a > 5 else 0))
""".strip()

BRUTE = """
import sys

a, b = map(int, sys.stdin.read().split())
total = a
for _ in range(b):
    total += 1
print(total)
""".strip()

VALIDATOR = """
import sys

raw = sys.stdin.buffer.read().decode("utf-8")
lines = raw.rstrip("\\n").split("\\n")
if len(lines) != 1:
    sys.stderr.write("줄 수가 1이 아닙니다")
    sys.exit(1)
parts = lines[0].split(" ")
if len(parts) != 2:
    sys.stderr.write("토큰이 2개가 아닙니다")
    sys.exit(1)
for part in parts:
    if not part.isdigit():
        sys.stderr.write("정수가 아닙니다: " + part)
        sys.exit(1)
    if not 0 <= int(part) <= 1000:
        sys.stderr.write("범위를 벗어났습니다: " + part)
        sys.exit(1)
""".strip()

STRESS_GENERATOR = """
import random
import sys

random.seed(int(sys.argv[1]))
print(random.randint(0, 12), random.randint(0, 12))
""".strip()

RANDOM_GENERATOR = """
import random
import sys

random.seed(int(sys.argv[1]))
print(random.randint(0, 1000), random.randint(0, 1000))
""".strip()

SCALE_GENERATOR = """
import random
import sys

random.seed(int(sys.argv[1]))
print(random.randint(900, 1000), random.randint(900, 1000))
""".strip()

ANTI_GENERATOR = """
import random
import sys

random.seed(int(sys.argv[1]))
print(0, random.randint(0, 3))
""".strip()

INVALID_GENERATOR = """
import sys

print(5000, 5000)
""".strip()

MUTANT_SUBTRACT = """
import sys

a, b = map(int, sys.stdin.read().split())
print(a - b)
""".strip()

MUTANT_OFF_BY_ONE = """
import sys

a, b = map(int, sys.stdin.read().split())
print(a + b + 1)
""".strip()

MUTANT_SLOW = """
import sys

a, b = map(int, sys.stdin.read().split())
total = 0
for _ in range(60_000_000):
    total += 1
print(a + b)
""".strip()

MUTANT_HUNGRY = """
import sys

a, b = map(int, sys.stdin.read().split())
blob = [0] * (40 * 1024 * 1024)
print(a + b + len(blob) - len(blob))
""".strip()

#: 모든 초기 케이스를 통과하지만 1000 1000에서만 틀리는 오답
MUTANT_SNEAKY = """
import sys

a, b = map(int, sys.stdin.read().split())
if a == 1000 and b == 1000:
    print(0)
else:
    print(a + b)
""".strip()


def make_problem(**overrides: Any) -> Problem:
    """A+B 문제."""
    defaults = dict(
        slug=slugify("A+B"),
        title="A+B",
        statement="두 정수 A와 B를 입력받아 합을 출력한다.",
        input_spec="첫째 줄에 A와 B가 공백으로 구분되어 주어진다.",
        output_spec="A+B를 출력한다.",
        constraints="0 <= A, B <= 1000",
        examples=(Example(input="1 2\n", output="3\n"),),
        limits=Limits(time_ms=2000, memory_mb=256),
        case_plan=CasePlan(count=10, max_input_bytes=1024, stress_rounds=10),
    )
    defaults.update(overrides)
    return Problem(**defaults)


def edge_payload() -> dict:
    return {
        "cases": [
            {"label": "최소값", "input": "0 0\n"},
            {"label": "최대값", "input": "1000 1000\n"},
            {"label": "한쪽만 최대", "input": "0 1000\n"},
            {"label": "작은 값", "input": "1 1\n"},
        ]
    }


def generators_payload(*, invalid: bool = False) -> dict:
    return {
        "generators": [
            {"name": "random", "kind": "random", "source": RANDOM_GENERATOR},
            {
                "name": "scale",
                "kind": "scale",
                "source": INVALID_GENERATOR if invalid else SCALE_GENERATOR,
            },
            {"name": "anti", "kind": "anti", "source": ANTI_GENERATOR},
        ]
    }


def mutants_payload(*, sneaky: bool = False) -> dict:
    mutants = [
        {"name": "subtract", "kind": "wa", "flaw": "더하기 대신 빼기", "source": MUTANT_SUBTRACT},
        {"name": "offbyone", "kind": "wa", "flaw": "1을 더 더함", "source": MUTANT_OFF_BY_ONE},
    ]
    if sneaky:
        mutants.append(
            {
                "name": "sneaky",
                "kind": "wa",
                "flaw": "최대 입력에서만 틀림",
                "source": MUTANT_SNEAKY,
            }
        )
    return {"mutants": mutants}


def slow_mutants_payload() -> dict:
    return {
        "mutants": [
            {"name": "slow", "kind": "tle", "flaw": "불필요한 반복", "source": MUTANT_SLOW},
            {"name": "hungry", "kind": "mle", "flaw": "거대한 리스트", "source": MUTANT_HUNGRY},
        ]
    }


def anti_payload() -> dict:
    return {"cases": [{"label": "최대 입력", "input": "1000 1000\n"}]}


class FakeCLI:
    """태그로 응답을 돌려주는 가짜 CLI.

    값이 리스트면 호출할 때마다 하나씩 꺼낸다(수정 루프 검증용).
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = {key: list(value) if isinstance(value, list) else value
                           for key, value in responses.items()}
        self.calls: list[str] = []

    def ask_code(self, prompt: str, *, tag: str = "code") -> str:
        return self._take(tag)

    def ask_json(self, prompt: str, *, tag: str = "json") -> dict:
        value = self._take(tag)
        return json.loads(value) if isinstance(value, str) else value

    def _take(self, tag: str) -> Any:
        self.calls.append(tag)
        key = self._match(tag)
        value = self._responses[key]
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def _match(self, tag: str) -> str:
        if tag in self._responses:
            return tag
        for key in self._responses:
            if tag.startswith(key):
                return key
        raise KeyError(f"가짜 CLI에 '{tag}' 응답이 없습니다. 준비된 태그: {sorted(self._responses)}")


def default_responses(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "solution": SOLUTION,
        "brute": BRUTE,
        "validator": VALIDATOR,
        "stress-gen": STRESS_GENERATOR,
        "edge": edge_payload(),
        "generators": generators_payload(),
        "mutants": mutants_payload(),
        "anti": anti_payload(),
        "fix": SOLUTION,
    }
    responses.update(overrides)
    return responses
