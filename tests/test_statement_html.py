"""지문 서식 HTML: 허용 목록 정리, 원문 보존 확인, 내보내기 반영."""
from fixtures import FakeCLI
from tcgen import statement_html as sh

FIB = """다음 소스는 N번째 피보나치 수를 구하는 C++ 함수이다.

int fibonacci(int n) {
    if (n == 0) {
        printf("0");
        return 0;
    }
}

fibonacci(3)을 호출하면 fibonacci(2)와 fibonacci(1)을 호출한다. 1 ≤ N ≤ 40, 2^31"""

FIB_HTML = """<blockquote><p>다음 소스는 $N$번째 피보나치 수를 구하는 C++ 함수이다.</p></blockquote>
<pre><code>int fibonacci(int n) {
    if (n == 0) {
        printf("0");
        return 0;
    }
}</code></pre>
<p><code>fibonacci(3)</code>을 호출하면 <code>fibonacci(2)</code>와 <code>fibonacci(1)</code>을 호출한다.
$1 \\le N \\le 40$, $2^{31}$</p>"""


def test_sanitize_drops_disallowed_tags_and_attributes():
    html = ('<p onclick="x()">a<i>b</i><kbd>c</kbd><script>bad()</script>'
            r'<a href="javascript:x">d</a><span style="color:red\;">e</span></p>'
            '<table><tr><td colspan="2">f</td></tr></table><img src="x.png">')
    assert sh.sanitize(html) == (
        '<p>a<em>b</em>c<a>d</a><span>e</span></p>'
        '<table style="border-collapse:collapse"><tr><td style="border:1px solid #999;padding:4px 8px">f</td>'
        '</tr></table>')


def test_sanitize_escapes_code_text_and_closes_tags():
    assert sh.sanitize("<pre><code>if (a &lt; b) x</code>") == "<pre><code>if (a &lt; b) x</code></pre>"


def test_formatting_keeps_exact_words():
    texts = {"description": FIB, "input_description": "첫째 줄에 T가 주어진다.", "output_description": ""}
    cli = FakeCLI({"statement-html": {"description": FIB_HTML,
                                      "input_description": "<p>둘째 줄에 $T$가 주어진다.</p>"}})
    result = sh.format_sections(texts, cli)
    assert "<code>fibonacci(3)</code>" in result["sections"]["description"]
    # 낱말이 바뀐 섹션은 버리고 평문으로 돌아간다.
    assert result["sections"]["input_description"] is None
    assert result["rejected"] == ["input_description"]
    assert result["source_key"] == sh.source_key(texts)


def test_export_uses_formatted_html_only_for_current_statement(tmp_path):
    import io
    from PIL import Image
    from test_problem_export import export_metadata, ready_workspace
    from tcgen import images, problem_export
    from tcgen.models import Figure

    ws = ready_workspace(tmp_path, statement="fibonacci(3) 호출\n[그림 1]", figures=(Figure(ref="[그림 1]", description="그림"),))
    sink = io.BytesIO(); Image.new("RGB", (3, 3), "blue").save(sink, format="PNG")
    images.store(ws, 1, sink.getvalue())
    texts = problem_export.section_texts(ws.load_problem())
    styled = {"source_key": sh.source_key(texts),
              "sections": {"description": "<p><code>fibonacci(3)</code> 호출</p><p>[그림 1]</p>"}}
    payload, _ = problem_export.build_payload(ws, {**export_metadata(ws), "statement_html": styled})
    value = payload["description"]["value"]
    assert "<code>fibonacci(3)</code>" in value and '<img src="images/1.png"' in value
    # 지문이 바뀌어 키가 다르면 예전 서식을 쓰지 않는다.
    stale = {**styled, "source_key": "old"}
    payload, _ = problem_export.build_payload(ws, {**export_metadata(ws), "statement_html": stale})
    assert "<code>" not in payload["description"]["value"]


def test_original_display_width_is_kept_from_import_to_export(tmp_path):
    import io
    from PIL import Image
    from test_problem_export import export_metadata, ready_workspace
    from tcgen import boj, images, problem_export
    from tcgen.models import Figure

    counter = boj.FigureCounter()
    text = boj.html_to_text('<p><img src="a" style="width: 150px; height: 151px;"/><img src="b" width="80"/>'
                            '<img src="c"/></p>', figures=counter)
    assert text.split() == ["[그림", "1]", "[그림", "2]", "[그림", "3]"] and counter.widths == [150, 80, 0]

    ws = ready_workspace(tmp_path, statement="[그림 1] [그림 2]",
                         figures=(Figure(ref="[그림 1]", width=150), Figure(ref="[그림 2]")))
    for number in (1, 2):
        sink = io.BytesIO(); Image.new("RGB", (900, 900), "blue").save(sink, format="PNG")
        images.store(ws, number, sink.getvalue())
    payload, _ = problem_export.build_payload(ws, export_metadata(ws))
    value = payload["description"]["value"]
    assert 'alt="그림 1" width="150"' in value
    assert f'alt="그림 2" width="{problem_export.DEFAULT_FIGURE_WIDTH}"' in value


def test_inline_code_spacing_survives_and_code_language_class_is_kept():
    html = sh.sanitize('<pre><code class="lang-cpp">int a;</code></pre>'
                       '<p><code>f(3)</code>은 <code>f(2)</code>와 <code>f(1)</code> (첫)</p>')
    assert '<code class="lang-cpp">' in html
    assert sh.keep_code_spacing(html) == (
        '<pre><code class="lang-cpp">int a;</code></pre>'
        '<p><code>f(3)</code>은&nbsp;<code>f(2)</code>와&nbsp;<code>f(1)</code>&nbsp;(첫)</p>')
    assert sh.signature(sh.visible_text(sh.keep_code_spacing(html))) == sh.signature("int a; f(3)은 f(2)와 f(1) (첫)")
