"""문제 지문을 code-place 지문 HTML 규칙에 맞는 서식 있는 HTML로 바꾼다.

AI가 코드 블록·인라인 코드·표·목록·인용·수식을 입힌다. AI 응답은 그대로 믿지
않는다.

- 허용 목록 밖의 태그·속성은 지운다(서버가 지울 것을 미리 지워 결과를 예측 가능하게).
- 보이는 글자가 원문과 같은지 확인한다. 서식만 바뀌고 낱말은 하나도 바뀌지 않아야
  한다. 다르면 그 섹션은 쓰지 않고 평문 내보내기로 돌아간다.
"""
from __future__ import annotations

import hashlib
import json
import re
from html import escape, unescape
from html.parser import HTMLParser
from typing import Callable

PROMPT_VERSION = "3"
SECTIONS = ("description", "input_description", "output_description")

_VOID = {"br", "hr", "img"}
_ALLOWED = {
    "p", "br", "div", "span", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "u", "s", "small", "sup", "sub", "q", "font",
    "ul", "ol", "li", "blockquote", "code", "pre",
    "table", "caption", "thead", "tbody", "tr", "th", "td", "a",
}
# img는 앱이 [그림 N] 자리에 직접 넣는다. AI가 만든 img는 받지 않는다.
_RENAMED = {"i": "em", "del": "s"}
_COMMON_ATTRS = {"style", "class", "name"}
_TAG_ATTRS = {"a": {"href", "target", "rel", "title"}, "font": {"color"}}
_UNSAFE_STYLE = re.compile(r"\\|&#|/\*|\*/|expression|url\s*\(", re.I)
_CELL_STYLE = "border:1px solid #999;padding:4px 8px"
_DEFAULT_STYLES = {"table": "border-collapse:collapse", "th": _CELL_STYLE, "td": _CELL_STYLE}
_LATEX_COMMAND = re.compile(r"\\[A-Za-z]+")
_SIGNIFICANT = re.compile(r"[0-9A-Za-z가-힣]")


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
            return
        tag = _RENAMED.get(tag, tag)
        if self.skip or tag not in _ALLOWED:
            return
        kept = []
        allowed = _COMMON_ATTRS | _TAG_ATTRS.get(tag, set())
        for name, value in attrs:
            value = value or ""
            if name not in allowed or (name == "style" and _UNSAFE_STYLE.search(value)):
                continue
            if name == "href" and not re.match(r"https?://", value, re.I):
                continue
            kept.append((name, value))
        if tag in _DEFAULT_STYLES and not any(name == "style" for name, _ in kept):
            # 문제 화면에 표 기본 스타일이 없어 테두리를 직접 지정한다.
            kept.append(("style", _DEFAULT_STYLES[tag]))
        rendered = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in kept)
        self.out.append(f"<{tag}{rendered}{' /' if tag in _VOID else ''}>")
        if tag not in _VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
            return
        tag = _RENAMED.get(tag, tag)
        if self.skip or tag not in self.open:
            return
        while self.open:
            current = self.open.pop()
            self.out.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))

    def result(self) -> str:
        self.close()
        return "".join(self.out) + "".join(f"</{tag}>" for tag in reversed(self.open))


def sanitize(html: str) -> str:
    """허용 목록 밖의 태그·속성을 지우고, 글자는 이스케이프해 다시 쓴다."""
    parser = _Sanitizer()
    parser.feed(html)
    return parser.result()


_SPACE_BEFORE_CODE = re.compile(r" (?=<code[\s>])")
_SPACE_AFTER_CODE = re.compile(r"(?<=</code>) (?!</pre>)")


def keep_code_spacing(html: str) -> str:
    """인라인 코드 앞뒤 공백을 &nbsp;로 바꾼다.

    문제 사이트가 저장할 때 인라인 태그 옆 일반 공백을 지워
    "은<code>f(2)</code>와"처럼 붙어 보이게 되는 것을 막는다.
    """
    return _SPACE_AFTER_CODE.sub("&nbsp;", _SPACE_BEFORE_CODE.sub("&nbsp;", html))


def visible_text(html: str) -> str:
    return unescape(re.sub(r"<[^>]*>", "", html))


def signature(text: str) -> str:
    """서식과 무관한 낱말의 순서. 수식 명령(\\le 등)·기호·공백은 비교에서 뺀다."""
    return "".join(_SIGNIFICANT.findall(_LATEX_COMMAND.sub("", text)))


def source_key(texts: dict[str, str]) -> str:
    raw = json.dumps({"version": PROMPT_VERSION, **texts}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prompt(texts: dict[str, str]) -> str:
    sections = json.dumps(texts, ensure_ascii=False, indent=2)
    return f"""
아래 문제 지문의 각 섹션을 문제 사이트에 올릴 HTML로 서식만 입혀라.
**낱말·숫자·문장은 한 글자도 바꾸거나, 더하거나, 빼거나, 옮기거나, 번역하지 마라.**
제목·설명·요약을 새로 쓰지 마라. 원문 순서를 그대로 지켜라.

원문 표기:
- `^`, `_`는 원래 위·아래 첨자였다(`2^31`, `A_i`).
- 한 줄에 ` | `로 칸이 나뉜 연속된 줄은 원래 표였다(첫 줄은 대개 머리글).
- 들여쓰기된 여러 줄 코드·의사코드·아스키 그림은 원래 고정폭 블록이었다.
- 연속된 짧은 줄은 원래 목록 항목일 수 있다.
- `[그림 N]`은 그림 자리다. 글자 그대로 남겨라(앱이 그림으로 바꾼다).

서식 규칙:
- 문단은 <p>, 줄바꿈은 <br />.
- 여러 줄 코드·의사코드·아스키 그림은 <pre><code>…</code></pre>. 공백·들여쓰기를 그대로 둔다.
  실제 프로그래밍 언어 코드면 언어를 class로 적는다: <pre><code class="lang-cpp">.
  값은 lang-c, lang-cpp, lang-java, lang-python, lang-javascript, lang-go, lang-rust, lang-kotlin 등.
  의사코드·아스키 그림·언어를 알 수 없는 코드에는 class를 붙이지 않는다.
- 문장 안의 코드 조각(함수 호출 `fibonacci(3)`, 배열 `dp[i]`, 코드 속 식별자·문자열·출력해야 할
  정확한 문구)은 <code>…</code>. 같은 함수·식별자가 여러 번 나오면 매번 <code>로 감싼다.
- 수학 변수·식·범위는 KaTeX 0.10 수식: 문장 안 $N$, $1 \\le N \\le 10^5$, $2^{{31}}$, $A_i$.
  최신 KaTeX 전용 명령은 쓰지 않는다. 진짜 달러 기호는 \\$. <pre>/<code> 안에는 수식을 쓰지 않는다.
- 표는 <table><thead><tr><th>…</th></tr></thead><tbody><tr><td>…</td></tr></tbody></table>.
  첫 줄이 머리글이 아니면(그림을 나란히 놓은 표, 그림 아래 캡션 줄 등) <thead> 없이 <tbody>만 쓴다.
  colspan·rowspan은 쓰지 않는다.
- 목록은 <ul>/<ol><li>, 인용은 <blockquote>, 강조는 <strong>/<em>.
- 사용할 수 있는 태그: p br div span hr h1~h6 strong b em u s small sup sub q ul ol li blockquote
  code pre table caption thead tbody tr th td. 그 밖의 태그(i, kbd, mark, dl, img 등)와 백틱(`)은 쓰지 않는다.
- 본문과 코드 안의 <, >, &는 &lt; &gt; &amp;로 적는다.
- 서식이 필요 없는 섹션은 <p>로 문단만 나눈다. 빈 섹션은 빈 문자열.

섹션(JSON):
{sections}

JSON 객체 하나만 답하라. 키는 위 섹션 이름과 같고 값은 HTML 문자열이다.
""".strip()


def format_sections(
    texts: dict[str, str], cli, *, on_progress: Callable[[str], None] | None = None,
) -> dict:
    """섹션별 서식 HTML을 만든다. 원문 보존을 확인하지 못한 섹션은 None으로 둔다."""
    result: dict = {"source_key": source_key(texts), "sections": {}, "rejected": []}
    payload = cli.ask_json(prompt(texts), tag="statement-html")
    for name in SECTIONS:
        original = texts.get(name, "")
        candidate = payload.get(name) if isinstance(payload, dict) else None
        if not original.strip():
            result["sections"][name] = None
            continue
        html = keep_code_spacing(sanitize(candidate)) if isinstance(candidate, str) and candidate.strip() else ""
        if html and signature(visible_text(html)) == signature(original):
            result["sections"][name] = html
        else:
            result["sections"][name] = None
            result["rejected"].append(name)
    if result["rejected"] and on_progress:
        on_progress(f"서식 변환 중 원문과 달라진 섹션은 평문으로 내보냅니다: {', '.join(result['rejected'])}")
    return result
