# TC_generator

코딩테스트 문제의 지문·제약·힌트를 넣으면 **정답 코드 확보 → 엣지케이스 테스트케이스 생성 → 오답 코드로 강도 검증 → QingdaoU OnlineJudge 업로드용 zip 내보내기**까지 수행하는 로컬 전용 도구입니다.

요구사항 정의는 [PRD.md](PRD.md)에 있습니다.

## 설치

```bash
pip install -r requirements.txt
```

빌드 단계가 없습니다. npm·번들러·CDN을 쓰지 않으므로 폐쇄망에서도 동작합니다.

## 실행

```bash
python -m tcgen
```

`http://127.0.0.1:8765/`가 브라우저에 열립니다. 외부에 노출되지 않습니다.

| 옵션 | 설명 |
|---|---|
| `--port 8765` | 포트 변경 |
| `--no-browser` | 브라우저를 열지 않음 |
| `--host 127.0.0.1` | 바인딩 주소 (기본값 유지를 권장) |

## 필요한 것

| 항목 | 필수 | 없을 때 |
|---|---|---|
| Python 3.11+ | 필수 | — |
| Claude Code CLI (`claude`) | 필수 | LLM 단계가 모두 실패 |
| psutil | 권장 | 메모리가 '미측정'으로 표시 |
| Docker 또는 gcc/g++/javac | 선택 | 해당 언어는 SKIP, 격리 없이 실행 |

시작할 때 환경을 점검해 **무엇이 SKIP되는지** 콘솔과 화면 상단에 표시합니다.

## 사용 흐름

**위저드 모드** — 각 단계에서 사람이 확인합니다.

1. **문제 입력** — 폼에 직접 입력하거나, 지문을 통째로 붙여넣어 파싱한 뒤 확인·수정
2. **지문 해석 확인** — 동률 처리·경계 포함 여부 등 해석이 갈릴 지점을 확인
3. **정답 코드 확정** — 브루트포스를 따로 만들어 작은 랜덤 입력으로 대조. **통과해야 확정**
4. **테스트케이스 생성** — 경계 케이스는 직접 명시, 랜덤·대규모는 생성기 스크립트 실행
5. **강도 검증** — 오답·느린 풀이·메모리 과다 풀이를 만들어 테스트가 잡는지 확인
6. **내보내기** — zip 생성 후 다시 읽어 검증

**전문가 모드** — `전체 실행` 버튼으로 2~6단계를 한 번에 돌립니다. 해석 확인을 건너뛰면 리포트에 `미확인`으로 남습니다.

## 설계상 중요한 사항

### 안전장치

- **정답 코드가 확정되기 전에는 케이스 생성과 내보내기가 차단됩니다.** 틀린 테스트가 OJ에 올라가는 것을 구조적으로 막습니다.
- **기대 출력은 추측하지 않습니다.** 확정된 정답 코드를 실행해서 얻습니다.
- **모든 생성 입력은 validator를 통과해야 채택됩니다.**
- **LLM이 만든 코드도 신뢰하지 않습니다.** 실행 전 위험 패턴(`subprocess`, `socket`, `os.system` 등)을 정적 스캔하고, 걸리면 실행을 멈춥니다.

### 격리

Docker 실행기가 붙기 전까지는 **격리 없이 로컬에서 실행**됩니다. 화면 상단에 배너로 계속 표시되며, 리포트에도 기록됩니다. 타임아웃 시 프로세스 트리를 전부 종료하고, 출력 상한을 적용하며, 환경변수를 최소화합니다.

Windows에서 메모리는 psutil 폴링(약 15ms 간격)으로 재므로 **MLE 판정은 참고치**입니다. 리포트에 측정 방식이 항상 적힙니다.

### QingdaoU 포맷

공식 소스에서 확인한 규칙을 그대로 지킵니다.

| 규칙 | 내용 |
|---|---|
| 메타데이터 파일명 | `info.json`이 **아니라** 확장자 없는 `info` |
| 배치 | zip **루트**에 `N.in`/`N.out` (하위 폴더 불가) |
| 번호 | 1부터 **빈틈 없이** 연속 (결번 뒤 케이스는 서버가 버림) |
| 해시 | CRLF→LF 정규화 후 `rstrip`한 값의 md5 |
| 공백 | **파일 끝 공백만** 무시. 중간 줄의 줄끝 공백은 오답 처리 |

출력 파일은 항상 LF로 저장하고, 만든 zip을 다시 읽어 해시·번호 연속성·루트 배치를 재확인합니다.

## 작업 폴더

`workspaces/<문제slug>/` 하나가 문제 하나입니다. DB가 없으므로 폴더를 복사하면 그대로 이식됩니다.

```
problem.json          문제 정보
solutions/            sol.py, brute.py
validator.py          입력 검증기
gens/                 생성기 스크립트(시드 기록)
cases/                1.in, 1.out, …, index.json
mutants/              wa_*.py, tle_*.py, mle_*.py
reports/              report.md, run.json
export/               <slug>_testcases.zip
logs/                 LLM 호출 기록
```

## 개발

```bash
python -m pytest              # 전체 (약 2분)
python -m pytest -m "not slow"   # 빠른 테스트만
python -m pytest --cov=tcgen --cov-report=term-missing
```

테스트는 가짜 LLM을 쓰지만 실행·비교·패키징은 진짜로 돌립니다.

### 구조

| 모듈 | 역할 |
|---|---|
| `tcgen/compare.py` | OJ와 동일한 출력 비교 규칙 (가장 중요) |
| `tcgen/exporter.py` | `info`/zip 생성과 자체 검증 |
| `tcgen/runner/` | 실행기 어댑터 (`local` → 향후 `docker`/`wsl`) |
| `tcgen/safety.py` | 실행 전 정적 위험 패턴 스캔 |
| `tcgen/llm.py` | Claude CLI headless 호출 (도구 권한 차단) |
| `tcgen/engine/` | 정답 확정·케이스 생성·강도 검증·오케스트레이션 |
| `tcgen/server.py` | 로컬 HTTP API + SSE |
| `tcgen/static/` | 브라우저 UI (의존성 없음) |

### 확장 지점

- **다른 언어 추가** — `tcgen/runner/base.py`의 `Runner` 프로토콜을 구현하고 `LANGUAGE_REQUIREMENTS`에 등록
- **Docker 격리** — `DockerRunner`를 만들어 `isolated = True`로 두면 배너가 자동으로 사라짐
- **문서 업로드** — 텍스트 추출기를 붙여 `parse_statement`에 넘기면 됨
