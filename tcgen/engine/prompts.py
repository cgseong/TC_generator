"""LLM 프롬프트 빌더.

모든 프롬프트는 출력 형식을 명시한다. 앱이 받은 텍스트를 기계적으로 파싱하기
때문이다 (PRD F2-A2). 지문에 없는 제약을 지어내지 말라는 지시를 공통으로 넣는다.
"""

from __future__ import annotations

import json

from tcgen.models import Problem

_COMMON_RULES = """
공통 규칙:
- 표준 입력(stdin)에서 읽고 표준 출력(stdout)으로만 출력한다.
- 파일 입출력, 네트워크, 프로세스 실행을 절대 사용하지 않는다.
- 외부 라이브러리를 쓰지 않는다. 파이썬 표준 라이브러리만 쓴다.
- 지문에 없는 제약을 지어내지 않는다. 모호하면 가장 보수적으로 해석한다.
- 설명 문장 없이 요구한 형식만 출력한다.
""".strip()


def _problem_block(problem: Problem) -> str:
    examples = "\n\n".join(
        f"[예제 {index}]\n입력:\n{example.input}\n출력:\n{example.output}"
        for index, example in enumerate(problem.examples, start=1)
    )
    return f"""
[문제 제목]
{problem.title}

[문제 설명]
{problem.statement}

[입력 설명]
{problem.input_spec}

[출력 설명]
{problem.output_spec}

[제약 조건]
{problem.constraints}

[힌트]
{problem.hints or "(없음)"}

[제한]
시간 {problem.limits.time_ms}ms, 메모리 {problem.limits.memory_mb}MB

{examples or "[예제 없음]"}
""".strip()


def parse_statement(raw_text: str) -> str:
    """지문 전문을 구조화 항목으로 쪼갠다 (PRD F1-2)."""
    return f"""
다음은 코딩테스트 문제 지문 전문이다. 항목별로 분해해 JSON으로만 답하라.

{_COMMON_RULES}

출력 형식(JSON 객체 하나):
{{
  "title": "문제 제목",
  "statement": "문제 설명 본문",
  "input_spec": "입력 형식 설명",
  "output_spec": "출력 형식 설명",
  "constraints": "제약 조건(범위를 빠짐없이)",
  "hints": "힌트 또는 빈 문자열",
  "time_ms": 2000,
  "memory_mb": 256,
  "examples": [{{"input": "예제 입력 원문", "output": "예제 출력 원문"}}]
}}

지문에 시간·메모리 제한이 없으면 time_ms는 2000, memory_mb는 256으로 둔다.
예제가 없으면 examples는 빈 배열로 둔다. 원문 줄바꿈을 보존하라.

[지문 전문]
{raw_text}
""".strip()


def interpretation(problem: Problem) -> str:
    """지문 해석 요약. 사람이 확인할 지점을 뽑는다 (PRD F1-4)."""
    return f"""
다음 문제를 구현하기 전에, 해석을 확인받으려 한다.
입력 형식·출력 정의·경계 처리를 요약하고, 해석이 갈릴 수 있는 지점을 모두 지적하라.

{_COMMON_RULES}

출력 형식(JSON 객체 하나):
{{
  "summary": "입력 형식, 출력 정의, 처리 절차를 5줄 이내로 요약",
  "ambiguities": ["해석이 갈릴 수 있는 지점(동률 처리, 경계 포함 여부, 출력 형식 등)"],
  "assumptions": ["이 해석대로 구현할 때 전제하는 가정"]
}}

{_problem_block(problem)}
""".strip()


def solution(problem: Problem) -> str:
    """정답 코드."""
    return f"""
다음 문제의 정답 파이썬 코드를 작성하라.
시간·메모리 제한 안에서 최대 입력을 처리할 수 있는 효율적인 풀이여야 한다.

{_COMMON_RULES}

파이썬 코드 블록 하나만 출력하라.

{_problem_block(problem)}
""".strip()


def brute_force(problem: Problem) -> str:
    """브루트포스 코드. 정답 확정의 기준이 된다 (PRD F2-B3)."""
    return f"""
다음 문제의 **브루트포스(완전탐색)** 파이썬 코드를 작성하라.

이 코드는 속도가 아니라 **정확성**이 목적이다. 작은 입력에서 반드시 옳은 답을
내야 하며, 효율적인 풀이와 독립적으로 가장 단순하고 직관적인 방법으로 구현하라.
가능하면 완전탐색·전수조사를 쓰고, 똑똑한 최적화를 쓰지 마라.

{_COMMON_RULES}

파이썬 코드 블록 하나만 출력하라.

{_problem_block(problem)}
""".strip()


def validator(problem: Problem) -> str:
    """입력 검증기. 모든 생성 입력이 이것을 통과해야 한다 (PRD F2-C4)."""
    return f"""
다음 문제의 **입력 검증기**를 파이썬으로 작성하라.

표준 입력으로 테스트케이스 입력을 읽어 제약 조건을 모두 검사한다.
- 형식과 제약을 모두 만족하면 아무것도 출력하지 않고 종료 코드 0으로 끝낸다.
- 위반하면 위반 내용을 stderr에 한 줄로 쓰고 종료 코드 1로 끝낸다.
- 줄 수, 토큰 개수, 값의 범위, 자료형, 추가 공백·빈 줄까지 엄격하게 검사하라.

{_COMMON_RULES}

파이썬 코드 블록 하나만 출력하라.

{_problem_block(problem)}
""".strip()


def stress_generator(problem: Problem) -> str:
    """스트레스 대조용 작은 입력 생성기 (PRD F2-B4)."""
    return f"""
다음 문제의 **작은 입력 생성기**를 파이썬으로 작성하라.

- ``sys.argv[1]``로 정수 시드를 받아 ``random.seed(시드)``로 초기화한다.
  시드가 같으면 항상 같은 입력을 만들어야 한다.
- 브루트포스로 몇 밀리초 안에 풀 수 있을 만큼 **아주 작은** 입력을 만든다.
  (예: N을 1~8 범위에서 뽑는다)
- 제약 조건을 반드시 지키고, 경계값이 자주 나오도록 분포를 잡는다.
- 입력 원문만 stdout에 출력한다.

{_COMMON_RULES}

파이썬 코드 블록 하나만 출력하라.

{_problem_block(problem)}
""".strip()


def case_generators(problem: Problem) -> str:
    """랜덤·대규모 케이스 생성기 묶음 (PRD F2-C2)."""
    max_bytes = problem.case_plan.max_input_bytes
    return f"""
다음 문제의 테스트케이스 **생성기 스크립트**들을 작성하라.

각 생성기는 ``sys.argv[1]``로 정수 시드를 받아 ``random.seed(시드)``로 초기화하고,
입력 원문만 stdout에 출력한다. 시드가 같으면 항상 같은 입력을 만들어야 한다.
생성한 입력은 제약 조건을 반드시 지켜야 하며, 한 입력의 크기가
{max_bytes} 바이트를 넘지 않아야 한다.

다음 세 종류를 각각 하나씩 만들어라.
- "random": 제약 범위 전체에서 고르게 뽑는 보통 크기 입력
- "scale": 제약의 최대 크기에 가까운 대규모 입력(시간 제한을 시험한다)
- "anti": 흔한 잘못된 풀이를 저격하는 입력(정렬 누락, 경계 미처리, 오버플로우 유발 등)

{_COMMON_RULES}

출력 형식(JSON 객체 하나):
{{
  "generators": [
    {{"name": "random", "kind": "random", "source": "파이썬 코드 전문"}},
    {{"name": "scale", "kind": "scale", "source": "파이썬 코드 전문"}},
    {{"name": "anti", "kind": "anti", "source": "파이썬 코드 전문"}}
  ]
}}

{_problem_block(problem)}
""".strip()


def edge_cases(problem: Problem) -> str:
    """경계·특수 케이스를 입력 데이터로 직접 받는다 (PRD F2-C1)."""
    return f"""
다음 문제의 **경계·특수 테스트케이스 입력**을 직접 작성하라.

최소값, 최대값, 단일 원소, 모두 같은 값, 모두 다른 값, 정렬된/역정렬된 입력,
음수, 0, 동률, 중복, 자료형 경계 등 사람이 빠뜨리기 쉬운 경우를 노려라.
각 입력은 제약 조건을 반드시 지켜야 한다. 큰 입력은 여기 쓰지 말고 생성기에 맡긴다.

{_COMMON_RULES}

출력 형식(JSON 객체 하나):
{{
  "cases": [
    {{"label": "무엇을 노린 케이스인지 짧게", "input": "입력 원문(줄바꿈 포함)"}}
  ]
}}

8개에서 15개 사이로 만들어라.

{_problem_block(problem)}
""".strip()


def mutants(problem: Problem, solution_source: str) -> str:
    """오답 코드. 테스트 강도를 재는 자가 된다 (PRD F2-D)."""
    return f"""
다음 문제의 정답 코드가 있다. 이 테스트케이스가 충분히 강한지 재기 위해,
**일부러 틀린 코드(뮤턴트)** 를 만들어라.

다음 세 종류를 만든다.
- "wa": 논리가 틀린 코드. off-by-one, 정렬 누락, 경계 미처리, 초기화 오류,
  동률 처리 실수 등 실제 제출에서 흔한 실수를 하나씩 심는다.
- "tle": 답은 맞지만 너무 느린 코드. 최대 입력에서 {problem.limits.time_ms}ms를
  넘도록 일부러 비효율적으로 짠다.
- "mle": 답은 맞지만 메모리를 과도하게 쓰는 코드.
  {problem.limits.memory_mb}MB를 넘도록 짠다.

각 뮤턴트는 예제 입력 정도는 통과할 수도 있어야 한다(너무 노골적으로 망가뜨리지 마라).

{_COMMON_RULES}

출력 형식(JSON 객체 하나):
{{
  "mutants": [
    {{"name": "off_by_one", "kind": "wa", "flaw": "무엇을 틀리게 했는지",
      "source": "파이썬 코드 전문"}}
  ]
}}

wa를 4개 이상, tle를 1개, mle를 1개 만들어라.

{_problem_block(problem)}

[정답 코드]
```python
{solution_source}
```
""".strip()


def fix_solution(
    problem: Problem,
    source: str,
    counterexample: str,
    solution_output: str,
    brute_output: str,
) -> str:
    """스트레스 대조에서 나온 반례로 정답 코드를 고친다 (PRD F2-B5)."""
    return f"""
아래 풀이가 브루트포스와 다른 답을 내는 반례가 발견되었다. 코드를 고쳐라.

{_COMMON_RULES}

파이썬 코드 블록 하나만 출력하라.

{_problem_block(problem)}

[현재 풀이]
```python
{source}
```

[반례 입력]
```
{counterexample}
```

[현재 풀이의 출력]
```
{solution_output}
```

[브루트포스의 출력(이쪽이 옳다고 가정)]
```
{brute_output}
```

브루트포스가 옳다는 전제로 풀이를 고쳐라. 다만 브루트포스 쪽이 지문을 잘못
해석했다고 판단되면, 코드 첫 줄에 ``# BRUTE_SUSPECT: 이유`` 주석을 남기고
지문에 맞는 올바른 풀이를 작성하라.
""".strip()


def anti_cases(problem: Problem, mutant_source: str, flaw: str) -> str:
    """살아남은 뮤턴트를 잡는 케이스를 추가로 만든다 (PRD F2-D5)."""
    return f"""
아래 **틀린 코드**가 현재 테스트케이스를 모두 통과해 버렸다.
이 코드를 반드시 틀리게 만드는 입력을 만들어라.

{_COMMON_RULES}

출력 형식(JSON 객체 하나):
{{
  "cases": [
    {{"label": "이 입력이 무엇을 드러내는지", "input": "입력 원문"}}
  ]
}}

3개에서 6개 사이로 만들어라. 제약 조건을 반드시 지켜야 한다.

{_problem_block(problem)}

[틀린 코드의 결함]
{flaw}

[틀린 코드]
```python
{mutant_source}
```
""".strip()


def describe_payload(payload: object) -> str:
    """재시도 프롬프트에 붙일 디버그 문자열."""
    return json.dumps(payload, ensure_ascii=False)[:500]
