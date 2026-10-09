"use strict";

/* TC_generator 브라우저 UI. 빌드 단계가 없도록 의존성 없이 작성한다. */

const state = {
  slug: null,
  problem: null,
  cases: [],
  stream: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ 통신 */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch (error) {
      /* 본문이 JSON이 아닐 수 있다 */
    }
    throw new Error(detail);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

function logLocal(message, level = "info") {
  appendLog({ stage: "ui", message, level, timestamp: Date.now() / 1000 });
}

/* ------------------------------------------------------------------ 환경 */

async function loadEnvironment() {
  const env = await api("/api/env");
  const badges = $("env-badges");
  badges.innerHTML = "";
  env.tools.forEach((tool) => {
    const chip = document.createElement("span");
    chip.className = `badge-chip ${tool.available ? "ok" : "missing"}`;
    chip.textContent = `${tool.name}${tool.available ? "" : " 없음"}`;
    chip.title = tool.detail || "";
    badges.appendChild(chip);
  });

  if (env.skipped_languages.length) {
    const chip = document.createElement("span");
    chip.className = "badge-chip missing";
    chip.textContent = `SKIP: ${env.skipped_languages.join(", ")}`;
    chip.title = "이 언어들은 검증되지 않습니다.";
    badges.appendChild(chip);
  }

  const banner = $("isolation-banner");
  banner.hidden = env.isolated;
  banner.textContent = `${env.banner} · 측정 방식 ${env.measurement}`;
  $("model-input").value = env.model || "";

  if (!env.llm_available) {
    logLocal("claude CLI를 찾지 못했습니다. LLM 단계는 실패합니다.", "error");
  }
}

/* ------------------------------------------------------------------ 목록 */

async function loadProblems() {
  const problems = await api("/api/problems");
  const list = $("problem-list");
  list.innerHTML = "";
  problems.forEach((item) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.className = item.slug === state.slug ? "active" : "";
    button.innerHTML =
      `${escapeHtml(item.title)}<span class="meta">케이스 ${item.case_count}건 · ` +
      `${item.solution_confirmed ? "정답 확정" : "미확정"}</span>`;
    button.onclick = () => selectProblem(item.slug);
    li.appendChild(button);
    list.appendChild(li);
  });
}

async function selectProblem(slug) {
  state.slug = slug;
  $("no-selection").hidden = true;
  $("workbench").hidden = false;
  connectStream(slug);
  await refresh();
  await loadProblems();
}

async function refresh() {
  if (!state.slug) return;
  const payload = await api(`/api/problems/${encodeURIComponent(state.slug)}`);
  state.problem = payload.problem;
  state.cases = payload.cases;
  renderProblem();
  renderCases();
  renderJob(payload.job);
  $("report-text").textContent = payload.report || "아직 리포트가 없습니다.";
  const link = $("download-link");
  link.hidden = !payload.has_zip;
  link.href = `/api/problems/${encodeURIComponent(state.slug)}/download`;
}

/* ------------------------------------------------------------------ 렌더 */

function renderProblem() {
  const problem = state.problem;
  $("f-title").value = problem.title || "";
  $("f-statement").value = problem.statement || "";
  $("f-input-spec").value = problem.input_spec || "";
  $("f-output-spec").value = problem.output_spec || "";
  $("f-constraints").value = problem.constraints || "";
  $("f-hints").value = problem.hints || "";
  $("f-time").value = problem.limits.time_ms;
  $("f-memory").value = problem.limits.memory_mb;
  $("f-case-count").value = problem.case_plan.count;
  $("f-max-bytes").value = problem.case_plan.max_input_bytes;
  $("f-stress").value = problem.case_plan.stress_rounds;
  $("workspace-path").textContent = `workspaces/${problem.slug}/`;

  renderExamples(problem.examples || []);
  setBadge("badge-interpret", problem.interpretation_confirmed ? "확인 완료" : "미확인",
    problem.interpretation_confirmed ? "done" : "");
  setBadge("badge-solution", problem.solution_confirmed ? "확정" : "미확정",
    problem.solution_confirmed ? "done" : "blocked");
  setBadge("badge-cases", `${state.cases.length}건`, state.cases.length ? "done" : "");
  $("interpretation").textContent = problem.interpretation || "";
  $("export-button").disabled = !problem.solution_confirmed;
  $("cases-button").disabled = !problem.solution_confirmed;
}

function renderExamples(examples) {
  const container = $("examples");
  container.innerHTML = "";
  examples.forEach((example, index) => container.appendChild(exampleRow(example, index)));
  if (!examples.length) container.appendChild(exampleRow({ input: "", output: "" }, 0));
}

function exampleRow(example, index) {
  const row = document.createElement("div");
  row.className = "example-row";
  row.innerHTML =
    `<label>예제 ${index + 1} 입력<textarea rows="3" data-role="input">` +
    `${escapeHtml(example.input || "")}</textarea></label>` +
    `<label>출력<textarea rows="3" data-role="output">` +
    `${escapeHtml(example.output || "")}</textarea></label>`;
  const remove = document.createElement("button");
  remove.className = "secondary";
  remove.type = "button";
  remove.textContent = "삭제";
  remove.onclick = () => row.remove();
  row.appendChild(remove);
  return row;
}

function collectExamples() {
  return Array.from($("examples").querySelectorAll(".example-row"))
    .map((row) => ({
      input: row.querySelector('[data-role="input"]').value,
      output: row.querySelector('[data-role="output"]').value,
    }))
    .filter((example) => example.input.trim() !== "");
}

function renderCases() {
  const container = $("cases-table");
  if (!state.cases.length) {
    container.textContent = "아직 케이스가 없습니다.";
    return;
  }
  const rows = state.cases
    .map(
      (item) =>
        `<tr><td><button class="secondary" data-case="${item.index}">${item.index}</button></td>` +
        `<td>${escapeHtml(item.label)}</td><td>${escapeHtml(item.origin || "-")}</td>` +
        `<td>${item.input_bytes.toLocaleString()}B</td>` +
        `<td>${item.output_bytes.toLocaleString()}B</td></tr>`
    )
    .join("");
  container.innerHTML =
    `<table><thead><tr><th>번호</th><th>종류</th><th>출처</th><th>입력</th><th>출력</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>`;
  container.querySelectorAll("[data-case]").forEach((button) => {
    button.onclick = () => showCase(button.dataset.case);
  });
}

async function showCase(index) {
  const payload = await api(`/api/problems/${encodeURIComponent(state.slug)}/case/${index}`);
  const preview = $("case-preview");
  preview.hidden = false;
  preview.textContent = `--- ${index}.in ---\n${payload.input}\n--- ${index}.out ---\n${payload.output}`;
}

function renderJob(job) {
  $("job-status").textContent = job.running ? `${job.name} 실행 중…` : "";
  $("cancel-button").hidden = !job.running;
  document.querySelectorAll(".step-body button").forEach((button) => {
    if (button.classList.contains("secondary")) return;
    button.disabled = job.running;
  });
  if (!job.running && state.problem) {
    $("export-button").disabled = !state.problem.solution_confirmed;
    $("cases-button").disabled = !state.problem.solution_confirmed;
  }
}

function setBadge(id, text, variant) {
  const badge = $(id);
  badge.textContent = text;
  badge.className = `badge ${variant || ""}`;
}

/* -------------------------------------------------------------------- SSE */

function connectStream(slug) {
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/api/problems/${encodeURIComponent(slug)}/events`);
  $("log").innerHTML = "";
  state.stream.onmessage = (message) => {
    const event = JSON.parse(message.data);
    appendLog(event);
    if (isTerminal(event)) refresh().catch((error) => logLocal(error.message, "error"));
  };
  state.stream.onerror = () => logLocal("로그 연결이 끊겼습니다. 새로고침하세요.", "warn");
}

function isTerminal(event) {
  return ["success", "error"].includes(event.level);
}

function appendLog(event) {
  const list = $("log");
  const li = document.createElement("li");
  li.className = event.level || "info";
  const time = new Date((event.timestamp || Date.now() / 1000) * 1000);
  li.innerHTML =
    `<span class="stage">${time.toLocaleTimeString()} ${escapeHtml(event.stage)}</span>` +
    escapeHtml(event.message);
  list.appendChild(li);
  list.scrollTop = list.scrollHeight;
}

/* ------------------------------------------------------------------ 동작 */

function guard(handler) {
  return async (clickEvent) => {
    if (clickEvent) clickEvent.preventDefault();
    try {
      await handler();
    } catch (error) {
      logLocal(error.message, "error");
    }
  };
}

async function post(path, body) {
  return api(`/api/problems/${encodeURIComponent(state.slug)}${path}`, {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
}

async function saveProblem() {
  await api(`/api/problems/${encodeURIComponent(state.slug)}`, {
    method: "PUT",
    body: JSON.stringify({
      title: $("f-title").value,
      statement: $("f-statement").value,
      input_spec: $("f-input-spec").value,
      output_spec: $("f-output-spec").value,
      constraints: $("f-constraints").value,
      hints: $("f-hints").value,
      examples: collectExamples(),
      time_ms: Number($("f-time").value),
      memory_mb: Number($("f-memory").value),
      case_count: Number($("f-case-count").value),
      max_input_bytes: Number($("f-max-bytes").value),
      stress_rounds: Number($("f-stress").value),
    }),
  });
  logLocal("문제 정보를 저장했습니다.", "success");
  await refresh();
  await loadProblems();
}

function bind() {
  $("new-problem-form").onsubmit = guard(async () => {
    const title = $("new-problem-title").value.trim();
    if (!title) return;
    const created = await api("/api/problems", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    $("new-problem-title").value = "";
    await selectProblem(created.slug);
  });

  $("save-button").onclick = guard(saveProblem);
  $("add-example").onclick = guard(async () => {
    $("examples").appendChild(exampleRow({ input: "", output: "" }, collectExamples().length));
  });

  $("parse-button").onclick = guard(async () => {
    const rawText = $("raw-text").value.trim();
    if (!rawText) throw new Error("붙여넣은 지문이 없습니다.");
    await post("/parse", { raw_text: rawText });
  });

  $("interpret-button").onclick = guard(() => post("/interpret"));
  $("confirm-interpret-button").onclick = guard(async () => {
    await post("/confirm-interpretation");
    await refresh();
  });

  $("solution-button").onclick = guard(() =>
    post("/solution", { author_source: $("author-source").value.trim() || null })
  );
  $("cases-button").onclick = guard(() => post("/cases"));
  $("strength-button").onclick = guard(() => post("/strength"));
  $("export-button").onclick = guard(() => post("/export"));
  $("run-all-button").onclick = guard(() => post("/run-all"));
  $("cancel-button").onclick = guard(() => post("/cancel"));

  $("save-settings").onclick = guard(async () => {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ model: $("model-input").value.trim() || null }),
    });
    logLocal("설정을 저장했습니다.", "success");
  });

  $("refresh-report").onclick = guard(refresh);
  $("clear-log").onclick = () => ($("log").innerHTML = "");

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll(".tab").forEach((other) => other.classList.remove("active"));
      tab.classList.add("active");
      ["wizard", "expert", "report"].forEach((name) => {
        $(`tab-${name}`).hidden = name !== tab.dataset.tab;
      });
    };
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

bind();
loadEnvironment().catch((error) => logLocal(error.message, "error"));
loadProblems().catch((error) => logLocal(error.message, "error"));
