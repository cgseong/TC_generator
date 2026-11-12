"use strict";

/* TC_generator 브라우저 UI. 빌드 단계가 없도록 의존성 없이 작성한다. */

const state = {
  view: "home",
  batchLoaded: false,
  followBatch: true,
  streamSlug: null,
  slug: null,
  problem: null,
  cases: [],
  stream: null,
  images: {},
  catalogOffset: 0,
  catalogTotal: 0,
  previewId: null,
  previewKey: null,
  previewTimer: null,
  imageDownload: null,
  catalogRequest: 0,
  authorDrafts: {},
  selected: new Map(),
  catalogPage: null,
  history: {},
  batch: null,
  batchTimer: null,
  batchStarting: false,
  batchSignature: null,
  provider: null,
  providerSaving: false,
  envRequest: 0,
  logEvents: [],
  logDetails: false,
  caseLog: null,
};

const $ = (id) => document.getElementById(id);
const CATALOG_PAGE_SIZE = 10;
const catalogPages = new Map();
const CATALOG_CACHE_MS = 30_000;

/* --------------------------------------------------------------- 화면 이동 */

function showView(view, navigate = true) {
  state.view = view === "execution" ? "execution" : "home";
  const execution = state.view === "execution";
  $("home-view").hidden = execution;
  $("execution-view").hidden = !execution;
  for (const [id, active] of [["home-button", !execution], ["execution-button", execution]]) {
    $(id).classList.toggle("active", active);
    if (active) $(id).setAttribute("aria-current", "page");
    else $(id).removeAttribute("aria-current");
  }
  if (navigate) {
    if (location.hash !== `#${state.view}`) history.pushState(null, "", `#${state.view}`);
    $(execution ? "execution-view" : "home-view").focus({preventScroll: true});
    window.scrollTo({top: 0});
  }
}

function renderExecutionEmpty() {
  $("execution-empty").hidden = Boolean(state.batch?.items?.length) || !$("workbench").hidden;
}

function highlightPreview() {
  document.querySelectorAll("[data-boj-id]").forEach((button) => {
    const active = Number(button.dataset.bojId) === state.previewId;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

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
      detail = Array.isArray(payload.detail)
        ? payload.detail.map((item) => `${(item.loc || []).slice(1).join(".")}: ${item.msg}`).join(" · ")
        : payload.detail || detail;
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
  const request = ++state.envRequest;
  const env = await api("/api/env");
  if (request !== state.envRequest) return;
  state.provider = env.provider || "claude";
  $("llm-provider").value = state.provider;
  const badges = $("env-badges");
  badges.innerHTML = "";
  $("env-summary").textContent = `실행 환경 ${env.tools.filter((tool) => tool.available).length}/${env.tools.length}`;
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
  state.modelOptions = env.model_options || {};
  renderLlmConfig(env.model, env.effort);
  $("drive-folder").value = env.drive_folder || "";
  $("drive-status").textContent = env.drive_credentials_available
    ? "서버 인증 설정됨 · 공개/접근 가능한 공유 폴더 사용 가능"
    : "공개 폴더 링크로 사용 · 비공개 폴더는 서버 인증 설정 필요";

  if (!env.llm_available) {
    logLocal(`${state.provider === "codex" ? "Codex" : "Claude"} CLI를 찾지 못했습니다. 다른 AI를 선택하거나 CLI를 설치해 주세요.`, "error");
  }
  refreshProviderStatus().catch((error) => { $("llm-status").textContent = error.message; });
}

function fillSelect(select, options, value) {
  select.innerHTML = "";
  options.forEach(([optionValue, label]) => {
    const option = document.createElement("option");
    option.value = optionValue; option.textContent = label;
    select.appendChild(option);
  });
  select.value = value || "";
}

function renderLlmConfig(model, effort) {
  const options = state.modelOptions?.[state.provider] || [];
  const choices = [["", state.provider === "codex" ? "기본값 (API 환산 비용 계산 불가)" : "기본값"],
    ...options.map((item) => [item.id, item.priced ? item.label : `${item.label} (단가 미상)`])];
  if (model && !options.some((item) => item.id === model)) choices.push([model, model]);
  fillSelect($("model-input"), choices, model);
  const chosen = options.find((item) => item.id === model);
  const levels = chosen ? chosen.efforts : [...new Set(options.flatMap((item) => item.efforts))];
  const fallback = chosen?.default_effort ? `기본값 (${chosen.default_effort})` : "기본값";
  fillSelect($("effort-input"), [["", fallback], ...levels.map((level) => [level, level])], effort);
}

function elapsedSeconds(start, end) {
  const from = Date.parse(start || "");
  const to = end ? Date.parse(end) : Date.now();
  return Number.isFinite(from) && Number.isFinite(to) ? Math.max(0, (to - from) / 1000) : null;
}

function formatDuration(seconds) {
  if (seconds == null) return "";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), sec = total % 60;
  return h ? `${h}시간 ${m}분 ${sec}초` : m ? `${m}분 ${sec}초` : `${sec}초`;
}

function formatUsd(value) {
  return `$${(value || 0).toFixed(value >= 1 ? 2 : 4)}`;
}

function formatUsage(usage) {
  if (!usage?.calls) return "";
  const n = (value) => (value || 0).toLocaleString("ko-KR");
  const cost = usage.unpriced_calls
    ? (usage.unpriced_calls === usage.calls ? "API 환산 비용 계산 불가(모델 지정 필요)"
      : `API 환산 ${formatUsd(usage.cost_usd)} 이상 (${usage.unpriced_calls}회 단가 미상)`)
    : `API 환산 ${formatUsd(usage.cost_usd)}`;
  const models = usage.models?.length ? ` · 모델 ${usage.models.join(", ")}` : "";
  return `${cost} · 토큰 ${n(usage.total)} (입력 ${n(usage.input)}, 그중 캐시 ${n(usage.cached)} · 출력 ${n(usage.output)}) · AI 호출 ${usage.calls}회${models}`;
}

async function refreshProviderStatus() {
  const requested = state.provider;
  $("llm-status").textContent = "로그인 상태 확인 중…";
  const status = await api("/api/llm-status");
  if (requested !== state.provider || status.provider !== state.provider) return;
  $("llm-status").textContent = status.message;
}

const llmControls = ["llm-provider", "model-input", "effort-input"];

async function saveLlmSettings(changes) {
  state.providerSaving = true;
  ++state.envRequest;
  llmControls.forEach((id) => { $(id).disabled = true; });
  $("save-settings").disabled = true;
  $("llm-status").textContent = "AI 설정을 저장하는 중…";
  renderSelection();
  try {
    const settings = await api("/api/settings", {method: "POST", body: JSON.stringify(changes)});
    const providerChanged = settings.provider !== state.provider;
    state.provider = settings.provider;
    renderLlmConfig(settings.model, settings.effort);
    if (providerChanged || "provider" in changes) await refreshProviderStatus();
    else $("llm-status").textContent = "";
    logLocal(`${state.provider === "codex" ? "GPT (ChatGPT 구독)" : "Claude Pro 구독"} · 모델 ${settings.model || "기본값"} · effort ${settings.effort || "기본값"}으로 저장했습니다. 진행 중인 작업은 시작할 때 선택한 설정으로 계속 실행합니다.`, "success");
  } catch (error) {
    $("llm-status").textContent = error.message;
    logLocal(error.message, "error");
    await loadEnvironment().catch(() => {});
  } finally {
    state.providerSaving = false;
    llmControls.forEach((id) => { $(id).disabled = false; });
    $("save-settings").disabled = false;
    renderSelection();
  }
}

function bindProviders() {
  state.provider = $("llm-provider").value || "claude";
  $("llm-provider").onchange = (event) => saveLlmSettings({provider: event.target.value});
  $("model-input").onchange = (event) => saveLlmSettings({model: event.target.value || null});
  $("effort-input").onchange = (event) => saveLlmSettings({effort: event.target.value || null});
  $("llm-check").onclick = guard(refreshProviderStatus);
}

/* ------------------------------------------------------------------ 목록 */

/* 문제 하나의 진행 상태. 생성 단계는 앱이, 업로드는 사람이 정한다. */
/* 용어
 * 미생성: zip을 아직 만들지 않음 · 생성 실패: 실행했지만 zip을 못 만듦
 * 재생성 필요: 문제를 고쳐 기존 zip이 맞지 않음 · 업로드 대기: zip이 있고 아직 업로드하지 않음
 * 재업로드 필요: 업로드 완료 뒤 zip을 다시 만듦 · 업로드 완료: 지금 zip을 문제 사이트에 올렸다고 기록됨
 * 업로드 완료만 사람이 정하고, 나머지는 생성 결과로 자동으로 정해진다. */
const STATUS = {
  none: {label: "미생성", tone: "gray"},
  failed: {label: "생성 실패", tone: "red"},
  rebuild: {label: "재생성 필요", tone: "red"},
  ready: {label: "업로드 대기", tone: "blue"},
  stale: {label: "재업로드 필요", tone: "orange"},
  uploaded: {label: "업로드 완료", tone: "green"},
};
const STATUS_GROUPS = [
  {title: "직접 기록", keys: ["uploaded"]},
  {title: "업로드 전 · 자동", keys: ["ready", "stale"]},
  {title: "생성 전 · 자동", keys: ["none", "failed", "rebuild"]},
];

const SPJ_TITLE = "스페셜 저지 문제: 정답이 여러 개일 수 있어 출력 글자 비교로는 맞는 답을 틀렸다고 채점할 수 있습니다.";

function spjLabel() {
  return `<span class="problem-label spj" title="${SPJ_TITLE}">SPJ</span>`;
}

function problemStatus(record) {
  const upload = record?.upload?.status;
  if (upload === "uploaded" || upload === "stale") return upload;
  if (record?.exported) return "ready";
  if (record?.status === "failed") return "failed";
  if (record?.status === "needs_review") return "rebuild";
  return "none";
}

function pillMarkup(key) {
  return `<span class="dot" aria-hidden="true"></span>${escapeHtml(STATUS[key].label)}`;
}

function statusTitle(key, record) {
  const when = record?.upload?.uploaded_at ? `${formatDate(record.upload.uploaded_at)} 업로드 완료 기록. ` : "";
  if (key === "stale") return `${when}그 뒤 zip을 다시 만들었습니다. 새 zip을 내려받아 다시 업로드하세요.`;
  if (key === "uploaded") return `${when}눌러서 상태를 바꿀 수 있습니다.`;
  if (key === "ready") return "zip이 있습니다. 내려받아 문제 사이트에 업로드한 뒤 업로드 완료로 기록하세요.";
  if (key === "rebuild") return "문제를 수정해서 기존 zip이 맞지 않습니다. 다시 실행해 zip을 만드세요.";
  return "눌러서 상태를 바꿀 수 있습니다.";
}

function statusPill(number, record) {
  const key = problemStatus(record);
  const button = document.createElement("button");
  button.type = "button";
  button.className = `status-pill tone-${STATUS[key].tone}`;
  button.innerHTML = pillMarkup(key);
  button.title = statusTitle(key, record);
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-label", `${number} 상태 ${STATUS[key].label}, 눌러서 변경`);
  button.onclick = (event) => {
    event.stopPropagation();
    openStatusMenu(button, number, record, key);
  };
  return button;
}

function closeStatusMenu(restoreFocus = false) {
  const menu = $("status-menu");
  if (menu.hidden) return;
  menu.hidden = true;
  if (restoreFocus) state.statusMenuAnchor?.focus();
  state.statusMenuAnchor = null;
}

function openStatusMenu(anchor, number, record, current) {
  const menu = $("status-menu");
  if (!menu.hidden && state.statusMenuAnchor === anchor) { closeStatusMenu(true); return; }
  state.statusMenuAnchor = anchor;
  menu.innerHTML = "";
  const head = document.createElement("div"); head.className = "status-menu-current";
  head.innerHTML = `<span class="status-pill tone-${STATUS[current].tone}">${pillMarkup(current)}</span>`;
  menu.appendChild(head);
  STATUS_GROUPS.forEach((group) => {
    const title = document.createElement("div"); title.className = "status-menu-group"; title.textContent = group.title;
    menu.appendChild(title);
    group.keys.forEach((key) => {
      const item = document.createElement("button");
      item.type = "button"; item.className = "status-menu-item"; item.setAttribute("role", "menuitem");
      item.innerHTML = `<span class="status-pill tone-${STATUS[key].tone}">${pillMarkup(key)}</span>` +
        (key === current ? '<span class="status-menu-check" aria-label="현재 상태">✓</span>' : "");
      // 사람이 고를 수 있는 건 "업로드 완료"뿐이다. 나머지는 생성 결과로 정해진다.
      const selectable = key === "uploaded" && current !== "uploaded";
      item.disabled = !selectable;
      if (!selectable && key !== current) item.title = "앱이 생성 결과에 따라 자동으로 정합니다.";
      if (selectable) item.onclick = guard(() => setUploaded(number, true));
      menu.appendChild(item);
    });
  });
  if (current === "uploaded" || current === "stale") {
    const rule = document.createElement("hr"); menu.appendChild(rule);
    const clear = document.createElement("button");
    clear.type = "button"; clear.className = "status-menu-item danger-text"; clear.setAttribute("role", "menuitem");
    clear.textContent = "업로드 완료 취소";
    clear.onclick = guard(async () => {
      if (!window.confirm(`${number}번의 업로드 완료 기록을 취소할까요?\n\n문제 사이트에 올라간 문제는 그대로이고, 이 앱의 기록만 지웁니다.`)) return;
      await setUploaded(number, false);
    });
    menu.appendChild(clear);
  }
  menu.hidden = false;
  const box = anchor.getBoundingClientRect();
  const width = menu.offsetWidth, height = menu.offsetHeight;
  const left = Math.min(Math.max(8, box.right - width), window.innerWidth - width - 8);
  const below = box.bottom + 6 + height <= window.innerHeight - 8;
  menu.style.left = `${left}px`;
  menu.style.top = `${below ? box.bottom + 6 : Math.max(8, box.top - height - 6)}px`;
  menu.querySelector(".status-menu-item:not(:disabled)")?.focus();
}

async function setUploaded(number, uploaded) {
  closeStatusMenu();
  const updated = await api(`/api/boj/${number}/upload`, {method: "PUT", body: JSON.stringify({uploaded})});
  state.history[String(number)] = updated;
  logLocal(uploaded ? `${number}번을 업로드 완료로 기록했습니다.` : `${number}번의 업로드 완료 기록을 취소했습니다.`, "success");
  await refreshUploadViews();
}

function bindStatusMenu() {
  document.addEventListener("click", (event) => {
    if (!$("status-menu").contains(event.target)) closeStatusMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeStatusMenu(true);
  });
  window.addEventListener("resize", () => closeStatusMenu());
  window.addEventListener("scroll", () => closeStatusMenu(), true);
}

async function refreshUploadViews() {
  catalogPages.clear();
  await loadHistory();
  // 상태가 바뀌면 필터에 걸리는 문제와 개수도 달라진다.
  await loadCatalog(true);
  await loadUploadLog();
}

/* 내려받을 zip을 서버에 기록한 뒤 내려받고, 올렸는지 확인받는다. */
async function downloadForUpload(target) {
  const result = await api("/api/upload-manifests", {method: "POST", body: JSON.stringify(target)});
  const link = document.createElement("a");
  link.href = result.download_url; link.download = "";
  document.body.appendChild(link); link.click(); link.remove();
  if (!result.manifest) return;
  await loadUploadLog();
  openUploadDialog(result.manifest);
}

function openUploadDialog(manifest) {
  state.uploadManifest = manifest;
  const confirmed = new Set(manifest.confirmed || []);
  const list = $("upload-dialog-items"); list.innerHTML = "";
  manifest.items.forEach((item) => {
    const li = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input"); box.type = "checkbox";
    box.value = item.problem_id;
    box.checked = !confirmed.has(item.problem_id);
    box.disabled = confirmed.has(item.problem_id);
    box.onchange = updateUploadDialog;
    const text = document.createElement("span");
    text.textContent = `${item.problem_id} · ${item.title || "제목 없음"}${confirmed.has(item.problem_id) ? " (업로드 완료 기록됨)" : ""}`;
    label.append(box, text); li.appendChild(label); list.appendChild(li);
  });
  $("upload-dialog-when").textContent = `${formatDate(manifest.created_at)}에 내려받은 ${manifest.kind === "batch" ? "묶음" : "문제"} zip · ${manifest.items.length}개`;
  $("upload-dialog-error").textContent = "";
  updateUploadDialog();
  $("upload-dialog").showModal();
}

function uploadDialogChecks() {
  return [...document.querySelectorAll("#upload-dialog-items input:not(:disabled)")];
}

function updateUploadDialog() {
  const count = uploadDialogChecks().filter((box) => box.checked).length;
  $("upload-confirm").textContent = count ? `${count}개 업로드 완료로 기록` : "기록할 문제를 체크하세요";
  $("upload-confirm").disabled = !count;
  $("upload-toggle-all").textContent = uploadDialogChecks().every((box) => box.checked) ? "모두 해제" : "모두 체크";
}

function bindUploadDialog() {
  $("upload-toggle-all").onclick = () => {
    const checks = uploadDialogChecks();
    const next = !checks.every((box) => box.checked);
    checks.forEach((box) => { box.checked = next; });
    updateUploadDialog();
  };
  $("upload-later").onclick = () => $("upload-dialog").close();
  $("upload-confirm").onclick = async () => {
    const manifest = state.uploadManifest;
    const ids = uploadDialogChecks().filter((box) => box.checked).map((box) => Number(box.value));
    if (!manifest || !ids.length) return;
    $("upload-confirm").disabled = true;
    try {
      await api(`/api/upload-manifests/${encodeURIComponent(manifest.id)}/confirm`,
        {method: "POST", body: JSON.stringify({problem_ids: ids})});
      $("upload-dialog").close();
      logLocal(`${ids.length}개를 업로드 완료로 기록했습니다.`, "success");
      await refreshUploadViews();
    } catch (error) {
      $("upload-dialog-error").textContent = error.message;
      updateUploadDialog();
    }
  };
}

async function loadUploadLog() {
  const {manifests} = await api("/api/upload-manifests?limit=10");
  $("upload-log").hidden = !manifests.length;
  const list = $("upload-log-items"); list.innerHTML = "";
  manifests.forEach((manifest) => {
    const done = manifest.confirmed.length, total = manifest.items.length;
    const li = document.createElement("li");
    li.dataset.state = done === total ? "done" : done ? "partial" : "pending";
    const title = document.createElement("strong");
    const progress = done === total ? "✓ 모두 업로드 완료" : done ? `${done}/${total}개 업로드 완료` : "업로드 기록 전";
    title.textContent = `${formatDate(manifest.created_at)} · ${manifest.kind === "batch" ? "묶음" : "문제"} ${total}개 · ${progress}`;
    const detail = document.createElement("div"); detail.className = "hint";
    detail.textContent = manifest.items.map((item) => `${item.problem_id} ${item.title || ""}`.trim()).join(", ");
    li.append(title, detail);
    if (done < total) {
      const record = document.createElement("button"); record.className = "secondary";
      record.textContent = "업로드 완료 기록";
      record.onclick = () => openUploadDialog(manifest);
      li.appendChild(record);
    }
    if (done) {
      const undo = document.createElement("button"); undo.className = "secondary";
      undo.textContent = "기록 취소";
      undo.onclick = guard(async () => {
        if (!window.confirm(`이 내려받기로 남긴 업로드 완료 기록 ${done}개를 취소할까요?\n\n그 뒤 다른 내려받기로 다시 기록한 문제는 그대로 둡니다.`)) return;
        await api(`/api/upload-manifests/${encodeURIComponent(manifest.id)}/undo`, {method: "POST"});
        logLocal(`업로드 완료 기록 ${done}개를 취소했습니다.`, "success");
        await refreshUploadViews();
      });
      li.appendChild(undo);
    }
    list.appendChild(li);
  });
}


async function loadProblems() {
  if (!$("saved-work").open) return;
  const problems = await api("/api/problems");
  const list = $("problem-list");
  list.innerHTML = "";
  problems.forEach((item) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.className = item.slug === state.slug ? "active" : "";
    button.innerHTML =
      `${escapeHtml(item.title)}<span class="meta">케이스 ${item.case_count}건 · ` +
      `${item.solution_confirmed ? "정답 확정" : "미확정"}</span>` +
      (item.history?.exported_at || item.history?.created_at
        ? `<span class="meta">${escapeHtml(formatDate(item.history.exported_at || item.history.created_at))}</span>` : "");
    button.title = item.history?.error || "";
    button.onclick = () => selectProblem(item.slug);
    li.appendChild(button);
    if (item.boj_id) {
      li.className = "saved-row";
      li.appendChild(statusPill(item.boj_id, state.history[String(item.boj_id)] || item.history));
    }
    list.appendChild(li);
  });
}

async function selectProblem(slug) {
  if (state.slug) state.authorDrafts[state.slug] = $("author-source").value;
  state.slug = slug;
  $("author-source").value = state.authorDrafts[slug] || "";
  $("case-preview").hidden = true;
  state.followBatch = false;
  $("workbench").hidden = false;
  renderExecutionEmpty();
  showView("execution");
  connectStream(slug);
  await refresh();
  await loadProblems();
}

async function refresh() {
  if (!state.slug) return;
  const slug = state.slug;
  const payload = await api(`/api/problems/${encodeURIComponent(slug)}`);
  if (state.slug !== slug) return;
  state.problem = payload.problem;
  state.cases = payload.cases;
  state.images = payload.images || {};
  state.imageDownload = payload.image_download;
  renderResults(payload.results || {});
  renderProblem();
  renderCases();
  renderJob(payload.job);
  $("report-text").textContent = payload.report || "아직 리포트가 없습니다.";
  const link = $("download-link");
  link.hidden = !payload.has_zip;
  link.href = `/api/problems/${encodeURIComponent(state.slug)}/download`;
  link.onclick = guard(() => downloadForUpload({slug: state.slug}));
}

function fetchCatalogPage(query, offset, force = false) {
  const status = state.catalogStatus || "all";
  const key = JSON.stringify([query, offset, status]);
  const cached = catalogPages.get(key);
  if (!force && cached && Date.now() - cached.time < CATALOG_CACHE_MS) return cached.promise;
  const entry = { time: Date.now() };
  entry.promise = api(`/api/boj?offset=${offset}&limit=${CATALOG_PAGE_SIZE}&q=${encodeURIComponent(query)}&status=${status}`)
    .catch((error) => {
      if (catalogPages.get(key) === entry) catalogPages.delete(key);
      throw error;
    });
  catalogPages.set(key, entry);
  if (catalogPages.size > 20) catalogPages.delete(catalogPages.keys().next().value);
  return entry.promise;
}

async function loadCatalog(force = false) {
  const request = ++state.catalogRequest;
  const offset = state.catalogOffset;
  const query = $("catalog-query").value.trim();
  const result = await fetchCatalogPage(query, offset, force);
  if (request !== state.catalogRequest) return;
  renderCatalog(result);
  if (offset + CATALOG_PAGE_SIZE < result.total) {
    fetchCatalogPage(query, offset + CATALOG_PAGE_SIZE).catch(() => {});
  }
}

const filterLabels = {all: "전체", uploaded: "업로드 완료", pending_upload: "업로드 대기", not_uploaded: "미업로드"};

function renderCatalogFilter(counts) {
  document.querySelectorAll("#catalog-filter [data-status]").forEach((button) => {
    const status = button.dataset.status;
    button.setAttribute("aria-pressed", String(status === (state.catalogStatus || "all")));
    const count = counts?.[status];
    button.textContent = Number.isInteger(count) ? `${filterLabels[status]} ${count.toLocaleString()}` : filterLabels[status];
  });
}

function bindCatalogFilter() {
  document.querySelectorAll("#catalog-filter [data-status]").forEach((button) => {
    button.onclick = guard(async () => {
      state.catalogStatus = button.dataset.status;
      state.catalogOffset = 0;
      renderCatalogFilter(state.catalogPage?.counts);
      await loadCatalog();
    });
  });
}

function renderCatalog(result) {
  state.catalogPage = result;
  renderCatalogFilter(result.counts);
  const offset = result.offset;
  const hasTotal = Number.isInteger(result.total);
  const hasMore = hasTotal ? offset + CATALOG_PAGE_SIZE < result.total : result.has_more;
  state.catalogTotal = result.total;
  $("catalog-status").textContent = !result.items.length
    ? ((state.catalogStatus || "all") === "all" ? "검색 결과가 없습니다." : `${filterLabels[state.catalogStatus]} 문제가 없습니다.`)
    : hasTotal ? `전체 ${result.total.toLocaleString()}개 · ${offset + 1}–${offset + result.items.length} · ${Math.floor(offset / CATALOG_PAGE_SIZE) + 1}/${Math.ceil(result.total / CATALOG_PAGE_SIZE)}페이지`
    : `${offset + 1}–${offset + result.items.length}개`;
  $("catalog-prev").disabled = offset === 0;
  $("catalog-next").disabled = !hasMore;
  const list = $("catalog-list");
  list.innerHTML = "";
  result.items.forEach((item) => {
    const li = document.createElement("li");
    li.className = "catalog-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.selectId = item.problem_id;
    checkbox.setAttribute("aria-label", `${item.problem_id} ${item.title} 일괄 실행 선택`);
    checkbox.checked = state.selected.has(item.problem_id);
    checkbox.onchange = () => toggleSelected(item, checkbox.checked);
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.bojId = item.problem_id;
    const record = state.history[String(item.problem_id)] || item.history;
    button.innerHTML = `<span class="problem-number">${escapeHtml(item.problem_id)}</span>` +
      `<span class="problem-name">${escapeHtml(item.title)}${item.special_judge ? spjLabel() : ""}</span>`;
    button.onclick = guard(() => previewBoj(item.problem_id));
    li.append(checkbox, button, statusPill(item.problem_id, record)); list.appendChild(li);
  });
  highlightPreview();
  renderSelection();
}

function hydrateCatalog() {
  const initial = $("catalog-list").dataset.initialPage;
  if (!initial) return false;
  renderCatalog(JSON.parse(initial));
  return true;
}


function formatDate(value) {
  return new Date(value).toLocaleString();
}

async function loadHistory() {
  const payload = await api("/api/history");
  state.history = payload.by_problem || {};
  if (state.catalogPage) renderCatalog(state.catalogPage);
  // 대기열의 상태 표시도 기록을 받은 뒤 다시 그린다.
  if (state.batch) renderBatch(state.batch);
  await loadProblems();
}

function restoreSelection() {
  try {
    const entries = JSON.parse(sessionStorage.getItem("tcgen-selected") || "[]");
    if (Array.isArray(entries)) entries.slice(0, 30).forEach((item) => {
      if (Number.isInteger(item.problem_id) && item.problem_id > 0) state.selected.set(item.problem_id, item);
    });
  } catch (_) { /* Storage can be unavailable in private browser sessions. */ }
}

function toggleSelected(item, selected) {
  if (selected && !state.selected.has(item.problem_id) && state.selected.size >= 30) {
    $("batch-notice").textContent = "한 번에 최대 30개까지 선택할 수 있습니다.";
  } else if (selected) state.selected.set(item.problem_id, item);
  else state.selected.delete(item.problem_id);
  renderSelection();
}

function renderSelection() {
  $("selection-count").textContent = `${state.selected.size} / 30개 선택`;
  const selected = $("selected-problems");
  selected.replaceChildren();
  state.selected.forEach((item) => {
    const chip = document.createElement("button");
    chip.className = "selection-chip";
    chip.textContent = `${item.problem_id} · ${item.title} ×`;
    chip.setAttribute("aria-label", `${item.problem_id} ${item.title} 선택 해제`);
    chip.onclick = () => toggleSelected(item, false);
    selected.appendChild(chip);
  });
  const previewSelected = state.selected.has(state.previewId);
  $("preview-select").textContent = previewSelected ? "실행 목록에서 제외" : "실행 목록에 추가";
  $("preview-select").setAttribute("aria-pressed", String(previewSelected));
  $("preview-select").disabled = !state.previewId || (!previewSelected && state.selected.size >= 30);
  $("batch-start").textContent = state.batch?.running ? "실행 상황 보기" : "선택한 문제 실행하기";
  $("batch-start").disabled = (!state.selected.size && !state.batch?.running) || state.batchStarting || state.providerSaving;
  document.querySelectorAll("[data-select-id]").forEach((checkbox) => {
    checkbox.checked = state.selected.has(Number(checkbox.dataset.selectId));
  });
  try { sessionStorage.setItem("tcgen-selected", JSON.stringify([...state.selected.values()].map(({problem_id, title}) => ({problem_id, title})))); }
  catch (_) { /* In-memory selection still works. */ }
}

const batchLabels = {
  pending: "대기", running: "실행 중", exported: "✓ 생성 완료", failed: "실패",
  skipped: "건너뜀 (이미 생성됨)", cancelled: "중단", interrupted: "서버 종료로 중단",
  done: "실행 완료", cancelling: "중단 중", idle: "대기",
};

function renderBatch(batch) {
  state.batch = batch;
  $("execution-indicator").hidden = !batch.running;
  renderExecutionEmpty();
  $("batch-panel").hidden = !batch.items?.length;
  $("batch-cancel").hidden = !batch.running;
  $("batch-cancel").disabled = batch.status === "cancelling";
  const counts = batch.counts || {};
  const finished = (batch.items || []).filter((item) => !["pending", "running"].includes(item.status)).length;
  $("batch-progress").max = batch.items?.length || 1;
  $("batch-progress").value = finished;
  $("batch-progress").setAttribute("aria-valuetext", `${batch.items?.length || 0}개 중 ${finished}개 처리`);
  const llm = batch.llm?.provider
    ? ` · ${batch.llm.provider === "codex" ? "GPT" : "Claude"} (모델 ${batch.llm.model || "기본값"}, effort ${batch.llm.effort || "기본값"})` : "";
  const used = (batch.items || []).filter((item) => item.usage?.calls);
  const totalCost = used.reduce((sum, item) => sum + (item.usage.cost_usd || 0), 0);
  const unpriced = used.some((item) => item.usage.unpriced_calls);
  const tokens = used.length
    ? ` · API 환산 합계 ${formatUsd(totalCost)}${unpriced ? " 이상" : ""} · 토큰 ${used.reduce((sum, item) => sum + item.usage.total, 0).toLocaleString("ko-KR")}` : "";
  const batchElapsed = formatDuration(elapsedSeconds(batch.created_at, batch.running ? null : batch.finished_at));
  const elapsed = batchElapsed ? ` · ${batch.running ? "경과" : "총 소요"} ${batchElapsed}` : "";
  $("batch-summary").textContent = `${batchLabels[batch.status] || batch.status}${elapsed}${llm}${tokens} · 전체 ${batch.items?.length || 0}개 · 내보내기 ${counts.exported || 0} · 건너뜀 ${counts.skipped || 0} · 실패 ${counts.failed || 0} · 대기 ${counts.pending || 0}`;
  const list = $("batch-items"); list.innerHTML = "";
  (batch.items || []).forEach((item) => {
    const li = document.createElement("li");
    li.dataset.status = item.status;
    const label = document.createElement("strong");
    const itemElapsed = item.started_at && (item.status === "running" || item.finished_at)
      ? formatDuration(elapsedSeconds(item.started_at, item.status === "running" ? null : item.finished_at)) : "";
    const timing = itemElapsed ? ` (${item.status === "running" ? `${itemElapsed} 경과` : `소요 ${itemElapsed}`})` : "";
    label.textContent = `${item.problem_id} · ${item.title} — ${batchLabels[item.status] || item.status}${timing}`;
    if (item.special_judge) label.insertAdjacentHTML("beforeend", spjLabel());
    label.className = item.status === "exported" || item.status === "skipped" ? "export-done" : "";
    li.appendChild(label);
    const detail = document.createElement("div"); detail.className = "hint";
    detail.textContent = item.error || item.message || item.stage || "";
    li.appendChild(detail);
    // 문제 하나가 끝난 뒤에만 그 문제에서 쓴 토큰을 보여 준다.
    if (!["pending", "running"].includes(item.status) && item.usage?.calls) {
      const usage = document.createElement("div"); usage.className = "hint batch-usage";
      usage.textContent = formatUsage(item.usage);
      li.appendChild(usage);
    }
    if (item.slug) {
      const button = document.createElement("button"); button.className = "secondary";
      button.textContent = "작업 열기"; button.onclick = guard(() => selectProblem(item.slug));
      li.appendChild(button);
      if (["exported", "skipped"].includes(item.status) && item.has_zip !== false) {
        const link = document.createElement("a"); link.className = "button-link";
        link.textContent = "zip 내려받기";
        link.href = `/api/problems/${encodeURIComponent(item.slug)}/download`;
        link.onclick = guard(() => downloadForUpload({slug: item.slug}));
        li.appendChild(link);
        li.appendChild(statusPill(item.problem_id, state.history[String(item.problem_id)]));
      }
    }
    list.appendChild(li);
  });
  const bundle = $("batch-download");
  bundle.hidden = !batch.bundle_count;
  bundle.href = `/api/batches/${encodeURIComponent(batch.id || "")}/download`;
  bundle.onclick = guard(() => downloadForUpload({batch_id: batch.id}));
  bundle.textContent = `완료 문제 ${batch.bundle_count || 0}개 묶음 내려받기`;
  $("batch-retry").hidden = batch.running || !(batch.items || []).some((item) => !["exported", "skipped"].includes(item.status));
  renderSelection();
}

async function pollBatch() {
  clearTimeout(state.batchTimer);
  try {
    const batch = await api("/api/batches/current");
    renderBatch(batch);
    if (!state.batchLoaded) {
      state.batchLoaded = true;
      if (batch.running && !location.hash) showView("execution");
    }
    const active = (batch.items || []).find((item) => item.status === "running" && item.slug);
    if (active && state.followBatch) connectStream(active.slug);
    const signature = JSON.stringify((batch.items || []).map((item) => [item.problem_id, item.status]));
    if (signature !== state.batchSignature) {
      state.batchSignature = signature;
      catalogPages.clear();
      loadHistory().catch((error) => logLocal(error.message, "warn"));
    }
    if (batch.running) state.batchTimer = setTimeout(pollBatch, 1000);
  } catch (error) {
    logLocal(`일괄 실행 상태 확인 실패: ${error.message}`, "warn");
    state.batchTimer = setTimeout(pollBatch, 3000);
  }
}

function bindBatch() {
  restoreSelection();
  $("select-page").onclick = () => {
    for (const item of state.catalogPage?.items || []) {
      if (state.selected.size >= 30) break;
      state.selected.set(item.problem_id, item);
    }
    renderSelection();
  };
  $("clear-selection").onclick = () => { state.selected.clear(); renderSelection(); };
  $("batch-start").onclick = guard(async () => {
    if (state.batch?.running) {
      showView("execution");
      return;
    }
    if (state.batchStarting || state.providerSaving || state.batch?.running || !state.selected.size) return;
    state.batchStarting = true;
    renderSelection();
    try {
      const batch = await api("/api/batches", { method: "POST", body: JSON.stringify({
        problem_ids: [...state.selected.keys()], skip_exported: $("skip-exported").checked,
      }) });
      renderBatch(batch);
      state.followBatch = true;
      if (state.stream) state.stream.close();
      state.stream = null;
      state.streamSlug = null;
      clearLog();
      $("workbench").hidden = true;
      renderExecutionEmpty();
      showView("execution");
      $("batch-notice").textContent = "순차 실행을 시작했습니다. 페이지를 새로고침해도 서버에서 계속 실행합니다.";
      await pollBatch();
    } finally {
      state.batchStarting = false;
      renderSelection();
    }
  });
  $("batch-cancel").onclick = guard(async () => {
    if (!state.batch?.running) return;
    renderBatch(await api(`/api/batches/${encodeURIComponent(state.batch.id)}/cancel`, {method: "POST"}));
    $("batch-notice").textContent = "중단을 요청했습니다. 현재 단계에서 중단을 처리한 뒤 나머지는 실행하지 않습니다.";
    await pollBatch();
  });
  $("batch-retry").onclick = () => {
    state.selected.clear();
    (state.batch.items || []).filter((item) => !["exported", "skipped"].includes(item.status))
      .forEach((item) => state.selected.set(item.problem_id, {problem_id: item.problem_id, title: item.title}));
    renderSelection();
    showView("home");
  };
  renderSelection();
}


async function previewBoj(number) {
  clearTimeout(state.previewTimer);
  state.previewKey = null;
  state.previewId = number;
  $("preview-notice").textContent = `${number}번 문제를 불러오는 중…`;
  $("catalog-preview").hidden = true;
  highlightPreview();
  let payload;
  try {
    payload = await api(`/api/boj/${number}`);
  } catch (error) {
    if (state.previewId === number) $("preview-notice").textContent = error.message;
    throw error;
  }
  if (state.previewId !== number) return;
  $("preview-notice").textContent = "";
  renderSelection();
  const problem = payload.problem;
  $("catalog-preview").hidden = false;
  $("catalog-close").hidden = false;
  $("preview-title").textContent = `${number} · ${problem.title}`;
  $("preview-limits").textContent = `시간 제한 ${problem.limits.time_ms.toLocaleString()}ms · 메모리 ${problem.limits.memory_mb}MB` + (problem.figures.length ? ` · 그림 ${problem.figures.length}개` : "");
  const figures = payload.figure_preview || { status: problem.figures.length ? "missing" : "none", images: {} };
  state.previewKey = figures.key;
  renderPreviewImages(figures);
  if (figures.status === "downloading") schedulePreviewImages(number, figures.key);
  const body = $("preview-body"); body.innerHTML = "";
  const sections = [["문제", problem.statement], ["입력", inputDescription(problem)], ["출력", problem.output_spec]];
  if (payload.notes.length) sections.push(["직접 보완할 항목", payload.notes.join("\n")]);
  sections.forEach(([title, text]) => {
    if (!text?.trim()) return;
    const h = document.createElement("h3"); h.textContent = title;
    const prose = document.createElement("div"); prose.className = "problem-prose";
    prose.textContent = text;
    body.append(h, prose);
  });
  (problem.examples || []).forEach((example, index) => {
    const heading = document.createElement("h3"); heading.textContent = `예제 ${index + 1}`;
    const sample = document.createElement("div"); sample.className = "sample-grid";
    for (const [label, value] of [["입력", example.input], ["출력", example.output]]) {
      const block = document.createElement("div");
      const title = document.createElement("h4"); title.textContent = label;
      const code = document.createElement("pre"); code.className = "sample-code"; code.textContent = value;
      block.append(title, code); sample.appendChild(block);
    }
    body.append(heading, sample);
  });
  if (state.view === "home" && window.matchMedia("(max-width: 900px)").matches) {
    $("catalog-preview").closest(".preview-pane").scrollIntoView({block: "start"});
  }
}

function renderPreviewImages(info) {
  const container = $("preview-images");
  container.innerHTML = "";
  Object.entries(info.images || {}).forEach(([number, url]) => {
    const figure = document.createElement("figure");
    const img = document.createElement("img");
    img.src = url; img.alt = `그림 ${number}`; img.className = "figure-image";
    const caption = document.createElement("figcaption"); caption.textContent = `그림 ${number}`;
    figure.append(img, caption); container.appendChild(figure);
  });
  const messages = {
    none: "", ready: `그림 ${Object.keys(info.images || {}).length}개를 불러왔습니다.`,
    downloading: "Drive에서 그림을 가져오는 중입니다… 지문은 먼저 확인할 수 있습니다.",
    missing: "로컬 그림이 없습니다. Drive 공유 폴더를 설정하거나 작업 시작 후 그림을 첨부하세요.",
    failed: `그림 다운로드 실패: ${info.error || "공유 권한과 파일을 확인하세요."}`,
  };
  $("preview-image-status").textContent = messages[info.status] || "";
}

function schedulePreviewImages(number, key) {
  state.previewTimer = setTimeout(async () => {
    if (state.previewId !== number || state.previewKey !== key) return;
    try {
      const info = await api(`/api/boj/${number}/figure-preview/${key}`);
      if (state.previewId !== number || state.previewKey !== key) return;
      renderPreviewImages(info);
      if (info.status === "downloading") schedulePreviewImages(number, key);
    } catch (error) {
      if (state.previewId === number && state.previewKey === key) {
        renderPreviewImages({ status: "failed", error: error.message, images: {} });
      }
    }
  }, 1000);
}

function closeCatalogPreview() {
  clearTimeout(state.previewTimer);
  state.previewId = null; state.previewKey = null;
  $("catalog-preview").hidden = true;
  $("catalog-close").hidden = true;
  $("preview-notice").textContent = "";
  highlightPreview();
  renderSelection();
  if (state.view === "home" && window.matchMedia("(max-width: 900px)").matches) {
    $("catalog-list").closest(".catalog-panel").scrollIntoView({block: "start"});
  }
}

async function uploadFigure(number, file) {
  await saveProblem();
  const form = new FormData(); form.append("file", file);
  await api(`/api/problems/${encodeURIComponent(state.slug)}/figures/${number}`, { method: "POST", headers: {}, body: form });
  await refresh();
  logLocal(`그림 ${number}번 첨부 완료. 전사를 확인하거나 AI로 읽어 주세요.`, "success");
}

function renderResults(results) {
  const stress = results.stress;
  $("solution-result").textContent = stress ? `${stress.passed ? "대조 통과" : "대조 실패"} · ${stress.rounds_run}회 · ${stress.note || ""}` : "";
  const strength = results.strength;
  setBadge("badge-strength", strength ? `${Math.round(strength.kill_rate * 100)}%` : "", strength?.survivor_count ? "blocked" : "done");
  $("strength-result").innerHTML = strength ? `<p>kill rate ${Math.round(strength.kill_rate * 100)}% · 생존 ${strength.survivor_count}건</p><table><thead><tr><th>유형</th><th>결함</th><th>결과</th></tr></thead><tbody>` + strength.results.map((r) => `<tr><td>${escapeHtml(r.kind)}</td><td>${escapeHtml(r.flaw)}</td><td>${r.killed ? `${escapeHtml(r.verdict)} · ${r.killed_by}번` : "생존"}</td></tr>`).join("") + "</tbody></table>" : "";
  const final = results.final;
  const finalText = final ? `일치 ${final.matched}건 · 실패 ${final.failed}건 · 미검증 ${final.skipped}건` : "아직 수행하지 않았습니다.";
  $("final-result").textContent = final
    ? `입력 검증 ${final.inputs_validated ?? 0}건 · 정답 출력 재검증 ${final.outputs_verified ?? 0}건 · ${finalText}`
    : finalText;
  setBadge("badge-final", final ? (final.fully_verified ? "전체 일치" : finalText) : "", final?.failed ? "blocked" : "done");
  const problemFormat = results.export?.format === "problem" || String(results.export?.zip_path || "").endsWith("_problem.zip");
  $("download-link").textContent = problemFormat ? "문제 zip 내려받기" : "이전 테스트케이스 zip 내려받기";
  $("export-result").textContent = results.export
    ? `${problemFormat ? "문제 zip" : "이전 테스트케이스 zip"} 내보내기 완료 · 테스트케이스 ${results.export.case_count}건`
    : "내보낼 때 최종 대조 후 문제 정보와 그림까지 함께 묶습니다.";
  const metadata = results.export_metadata;
  const container = $("export-metadata-result"); container.innerHTML = "";
  if (metadata) {
    const summary = document.createElement("p");
    summary.textContent = `${metadata.difficulty} (${metadata.difficulty_score}/16) · ${metadata.field_name} · 태그: ${(metadata.tags || []).join(", ")} · 출처: ${metadata.source}`;
    container.appendChild(summary);
    const rationale = document.createElement("details");
    const heading = document.createElement("summary"); heading.textContent = `난이도 평가 근거 · ${metadata.rubric_version}`;
    const text = document.createElement("pre"); text.className = "preview";
    const labels = {knowledge: "필요 지식", insight: "발상·모델링", implementation: "구현 복잡도", complexity: "효율화 난도"};
    const scale = metadata.difficulty_scale;
    const scaleText = Array.isArray(scale)
      ? scale.map((band) => `${band.min}~${band.max} ${band.name}`).join(" · ")
      : "이전 3단계 기준(0~4 Low · 5~10 Mid · 11~16 High) · 정보 생성 시 5단계로 재평가";
    text.textContent = `합계 ${scaleText}\n${metadata.algorithm || ""}\n` +
      Object.entries(metadata.assessment || {}).map(([key, value]) => `${labels[key] || key}: ${value.score}/4 — ${value.reason}`).join("\n");
    rationale.append(heading, text); container.appendChild(rationale);
    const note = document.createElement("p"); note.textContent = metadata.sample_note || ""; container.appendChild(note);
    (metadata.samples || []).forEach((sample, i) => {
      const title = document.createElement("h3"); title.textContent = `내보낼 예제 ${i + 1}`;
      const block = document.createElement("pre"); block.className = "preview";
      block.textContent = `입력\n${sample.input}\n출력\n${sample.output}`;
      container.append(title, block);
    });
  }
}

/* ------------------------------------------------------------------ 렌더 */

function inputDescription(problem) {
  const input = problem.input_spec || "";
  const legacy = (problem.constraints || "").trim();
  // 기존 작업과 DB의 별도 제한 섹션도 입력 설명에 포함해 내용은 보존한다.
  return legacy && !input.includes(legacy) ? [input, legacy].filter(Boolean).join("\n\n") : input;
}

function renderProblem() {
  const problem = state.problem;
  $("f-title").value = problem.title || "";
  $("f-statement").value = problem.statement || "";
  $("f-input-spec").value = inputDescription(problem);
  $("f-output-spec").value = problem.output_spec || "";
  $("f-hints").value = problem.hints || "";
  $("f-time").value = problem.limits.time_ms;
  $("f-memory").value = problem.limits.memory_mb;
  $("f-case-count").value = problem.case_plan.count;
  $("f-max-bytes").value = problem.case_plan.max_input_bytes;
  $("f-stress").value = problem.case_plan.stress_rounds;
  $("workspace-path").textContent = `workspaces/${problem.slug}/`;

  renderExamples(problem.examples || []);
  renderFigures(problem.figures || []);
  const download = state.imageDownload;
  $("figure-download-status").textContent = !download || download.status === "none" ? ""
    : download.status === "ready" ? `그림 ${download.available}/${download.expected}개 준비 완료`
    : download.status === "downloading" ? "Drive에서 그림을 가져오는 중입니다…"
    : download.status === "failed" ? `그림 다운로드 실패: ${download.error}`
    : "그림 파일을 첨부하거나 Drive에서 가져와 주세요.";
  $("pdf-status").textContent = problem.source_pdf
    ? `올린 PDF: ${problem.source_pdf}`
    : "올린 PDF가 없습니다.";
  setBadge("badge-solution", problem.solution_confirmed ? "확정" : "미확정",
    problem.solution_confirmed ? "done" : "blocked");
  setBadge("badge-cases", `${state.cases.length}건`, state.cases.length ? "done" : "");
  $("export-button").disabled = !problem.solution_confirmed;
  $("export-metadata-button").disabled = !problem.solution_confirmed || !state.cases.length;
  $("cases-button").disabled = !problem.solution_confirmed;
  $("final-button").disabled = !problem.solution_confirmed || !state.cases.length;
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

const FIGURE_ROLES = [
  ["spec", "spec · 입력 형식·좌표계 정의"],
  ["rule", "rule · 규칙 자체를 정의"],
  ["example_visual", "example_visual · 예제 시각화"],
  ["decorative", "decorative · 단순 삽화"],
];
const CRITICAL_ROLES = ["spec", "rule"];

function renderFigures(figures) {
  const container = $("figures");
  container.innerHTML = "";
  if (!figures.length) {
    container.textContent = "지문에서 인식된 그림이 없습니다.";
    setBadge("badge-figures", "없음", "");
    return;
  }
  figures.forEach((figure, index) => container.appendChild(figureRow(figure, index)));

  const unresolved = figures.filter(
    (figure) => CRITICAL_ROLES.includes(figure.role) && !(figure.description || "").trim()
  );
  setBadge(
    "badge-figures",
    unresolved.length ? `전사 필요 ${unresolved.length}건` : `${figures.length}건`,
    unresolved.length ? "blocked" : "done"
  );
}

function figureRow(figure, index) {
  const row = document.createElement("div");
  row.className = "figure-row";
  const options = FIGURE_ROLES.map(
    ([value, label]) =>
      `<option value="${value}"${figure.role === value ? " selected" : ""}>${label}</option>`
  ).join("");
  row.innerHTML =
    `<label>참조<input type="text" data-role="ref" ` +
    `value="${escapeHtml(figure.ref || `[그림${index + 1}]`)}"></label>` +
    `<label>역할<select data-role="role">${options}</select></label>` +
    `<label>전사<textarea rows="4" data-role="description">` +
    `${escapeHtml(figure.description || "")}</textarea></label>`;
  const media = document.createElement("div");
  const url = state.images[String(index + 1)];
  if (url) {
    const img = document.createElement("img");
    img.src = url;
    img.alt = figure.ref || `그림 ${index + 1}`;
    img.className = "figure-image";
    media.appendChild(img);
  }
  const upload = document.createElement("input");
  upload.type = "file";
  upload.accept = ".avif,.png,.jpg,.jpeg,.gif,.webp,.bmp";
  upload.setAttribute("aria-label", `${figure.ref} 교체`);
  upload.onchange = guard(async () => {
    if (upload.files[0]) await uploadFigure(index + 1, upload.files[0]);
  });
  media.appendChild(upload);
  row.appendChild(media);
  return row;
}

function collectFigures() {
  return Array.from($("figures").querySelectorAll(".figure-row"))
    .map((row) => ({
      ref: row.querySelector('[data-role="ref"]').value,
      role: row.querySelector('[data-role="role"]').value,
      description: row.querySelector('[data-role="description"]').value,
    }))
    .filter((figure) => figure.ref.trim() !== "");
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
  if (state.streamSlug === slug && state.stream) return;
  state.streamSlug = slug;
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/api/problems/${encodeURIComponent(slug)}/events`);
  clearLog();
  state.stream.onmessage = (message) => {
    if (state.streamSlug !== slug) return;
    const event = JSON.parse(message.data);
    appendLog(event);
    if (slug !== state.slug) return;
    if (isTerminal(event) || event.message.includes("중단했습니다")) {
      refresh().catch((error) => logLocal(error.message, "error"));
      if (event.stage === "export" || event.message.includes("단계를 마쳤습니다")) loadHistory().catch(() => {});
    }
  };
  state.stream.onerror = () => logLocal("로그 연결이 끊겼습니다. 새로고침하세요.", "warn");
}

function isTerminal(event) {
  return ["success", "error"].includes(event.level);
}

const LOG_STAGE_LABELS = {ui: "안내", llm: "AI", parse: "지문", solution: "정답", cases: "생성", strength: "강도", final: "최종", export: "내보내기", batch: "일괄", images: "그림"};

function logRow(event, message = event.message) {
  const li = document.createElement("li"); li.className = event.level || "info";
  const row = document.createElement("div"); row.className = "log-row";
  const time = document.createElement("span"); time.className = "log-time";
  time.textContent = new Date((event.timestamp || Date.now() / 1000) * 1000).toLocaleTimeString("ko-KR", {hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit"});
  const stage = document.createElement("span"); stage.className = "log-stage";
  stage.textContent = LOG_STAGE_LABELS[event.stage] || event.stage;
  const text = document.createElement("span"); text.className = "log-message"; text.textContent = message;
  row.append(time, stage, text); li.appendChild(row); return li;
}

function renderLogEvent(event) {
  const list = $("log");
  const data = event.data || {};
  const detail = !["warn", "error", "success"].includes(event.level) && event.stage === "cases"
    && (data.kind === "case_detail" || /^(생성|심사):/.test(event.message));
  const progress = data.kind === "case_progress";
  if (detail || progress) {
    if (!state.caseLog) state.caseLog = {generated: 0, reviewed: 0, accepted: null, target: null, rejected: 0, row: null};
    const group = state.caseLog;
    if (detail) {
      if (data.phase === "generate" || event.message.startsWith("생성:")) group.generated++;
      else group.reviewed++;
    }
    if (progress) {
      for (const key of ["generated", "reviewed", "accepted", "target", "rejected"]) {
        if (Number.isFinite(data[key])) group[key] = data[key];
      }
    }
    if (detail && state.logDetails) list.prepend(logRow(event));
    if (!detail || !state.logDetails) {
      const summary = group.accepted === null ? "테스트케이스 생성·검증 중" : `확정 ${group.accepted}/${group.target}개 · 제외 ${group.rejected}개`;
      const replacement = logRow(event, summary); replacement.classList.add("case-progress");
      const note = document.createElement("span"); note.className = "log-progress-note";
      note.textContent = `생성 ${group.generated}회 · 입력 심사 ${group.reviewed}회 · 생성기·seed는 상세 보기에서 확인`;
      replacement.querySelector(".log-message").appendChild(note);
      if (group.row?.isConnected) group.row.remove();
      group.row = replacement; list.prepend(replacement);
    }
  } else {
    list.prepend(logRow(event));
    if (event.stage !== "cases" || data.kind === "case_complete" || ["success", "error"].includes(event.level)) state.caseLog = null;
  }
  while (list.children.length > 400) list.lastElementChild.remove();
}

function appendLog(event) {
  const list = $("log");
  const follow = list.scrollTop < 50;
  const viewportTop = list.getBoundingClientRect().top;
  const anchor = follow ? null : Array.from(list.children).find((row) => row.getBoundingClientRect().bottom > viewportTop);
  const anchorTop = anchor?.getBoundingClientRect().top;
  const previousScrollTop = list.scrollTop;
  state.logEvents.push(event);
  if (state.logEvents.length > 2000) state.logEvents.shift();
  renderLogEvent(event);
  if (follow) list.scrollTop = 0;
  else if (anchor?.isConnected) list.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
  else list.scrollTop = previousScrollTop;
}

function clearLog() {
  state.logEvents = []; state.caseLog = null; $("log").innerHTML = "";
}

function toggleLogDetails() {
  state.logDetails = $("log-details").checked;
  state.caseLog = null; $("log").innerHTML = "";
  state.logEvents.forEach(renderLogEvent);
  $("log").scrollTop = 0;
}

/* ------------------------------------------------------------------ 동작 */

function guard(handler) {
  return async (clickEvent) => {
    if (clickEvent) clickEvent.preventDefault();
    try {
      $("app-notice").hidden = true;
      await handler();
    } catch (error) {
      logLocal(error.message, "error");
      $("app-notice").textContent = error.message;
      $("app-notice").hidden = false;
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
      constraints: "", // 기존 별도 필드의 내용은 위 입력 설명에 합쳐 저장한다.
      hints: $("f-hints").value,
      examples: collectExamples(),
      figures: collectFigures(),
      time_ms: Number($("f-time").value),
      memory_mb: Number($("f-memory").value),
      case_count: Number($("f-case-count").value),
      max_input_bytes: Number($("f-max-bytes").value),
      stress_rounds: Number($("f-stress").value),
    }),
  });
  logLocal("문제 정보를 저장했습니다.", "success");
  await refresh();
  await loadHistory();
}

function bindCatalog() {
  $("home-button").onclick = () => showView("home");
  $("execution-button").onclick = () => showView("execution");
  window.addEventListener("popstate", () => showView(location.hash.slice(1), false));
  showView(location.hash.slice(1), false);
  $("preview-select").onclick = () => {
    const item = state.catalogPage?.items.find((item) => item.problem_id === state.previewId)
      || {problem_id: state.previewId, title: $("preview-title").textContent.replace(/^\d+ · /, "")};
    if (state.previewId) toggleSelected(item, !state.selected.has(state.previewId));
  };
  bindBatch();
  bindProviders();
  bindUploadDialog();
  bindStatusMenu();
  bindCatalogFilter();
  $("saved-work").ontoggle = guard(loadProblems);
  $("catalog-search").onsubmit = guard(async () => { state.catalogOffset = 0; await loadCatalog(true); });
  $("catalog-prev").onclick = guard(async () => { state.catalogOffset = Math.max(0, state.catalogOffset - CATALOG_PAGE_SIZE); await loadCatalog(); });
  $("catalog-next").onclick = guard(async () => { state.catalogOffset += CATALOG_PAGE_SIZE; await loadCatalog(); });
  $("catalog-close").onclick = closeCatalogPreview;
  $("catalog-import").onclick = guard(async () => {
    if (!state.previewId) return;
    const created = await api("/api/problems/from-boj", { method: "POST", body: JSON.stringify({ problem_id: state.previewId }) });
    (created.notes || []).forEach((note) => logLocal(note, "warn"));
    closeCatalogPreview();
    await selectProblem(created.slug);
    await loadHistory();
  });
}

function bind() {
  $("save-button").onclick = guard(saveProblem);
  $("add-example").onclick = guard(async () => {
    $("examples").appendChild(exampleRow({ input: "", output: "" }, collectExamples().length));
  });

  $("parse-button").onclick = guard(async () => {
    const rawText = $("raw-text").value.trim();
    if (!rawText) throw new Error("붙여넣은 지문이 없습니다.");
    await post("/parse", { raw_text: rawText });
  });

  $("pdf-upload-button").onclick = guard(async () => {
    const chosen = $("pdf-file").files[0];
    if (!chosen) throw new Error("올릴 PDF를 선택하세요.");
    const form = new FormData();
    form.append("file", chosen);
    // Content-Type은 브라우저가 boundary와 함께 채우게 둔다.
    const result = await api(`/api/problems/${encodeURIComponent(state.slug)}/source-pdf`, {
      method: "POST",
      headers: {},
      body: form,
    });
    logLocal(`PDF를 올렸습니다 (${result.bytes.toLocaleString()}B).`, "success");
    await refresh();
  });

  $("pdf-parse-button").onclick = guard(() => post("/parse-pdf"));

  $("solution-button").onclick = guard(async () => {
    const author = $("author-source").value.trim() || null;
    await saveProblem();
    await post("/solution", { author_source: author });
  });
  $("cases-button").onclick = guard(() => post("/cases"));
  $("strength-button").onclick = guard(() => post("/strength"));
  $("export-button").onclick = guard(() => post("/export"));
  $("export-metadata-button").onclick = guard(() => post("/export-metadata"));
  $("run-all-button").onclick = guard(async () => {
    const author = $("author-source").value.trim() || null;
    await saveProblem();
    await post("/run-all", { author_source: author });
  });
  $("final-button").onclick = guard(() => post("/final"));
  $("figure-upload").onclick = guard(async () => {
    const file = $("figure-file").files[0];
    if (!file) throw new Error("첨부할 그림을 선택하세요.");
    await uploadFigure((state.problem.figures || []).length + 1, file);
    $("figure-file").value = "";
  });
  $("describe-figures").onclick = guard(async () => { await saveProblem(); await post("/describe-figures"); });
  $("drive-images").onclick = guard(() => post("/drive-images"));
  $("cancel-button").onclick = guard(() => post("/cancel"));

  $("save-settings").onclick = guard(async () => {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ drive_folder: $("drive-folder").value.trim() }),
    });
    logLocal("설정을 저장했습니다.", "success");
    await loadEnvironment();
  });

  $("refresh-report").onclick = guard(refresh);
  $("clear-log").onclick = clearLog;
  $("log-details").onchange = toggleLogDetails;

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

// First-page controls and content are independent of the rest of the workbench.
bindCatalog();
const hasInitialCatalog = hydrateCatalog();
try {
  bind();
} catch (error) {
  console.error(error);
  logLocal("화면 일부를 초기화하지 못했습니다. 새로고침해 주세요.", "error");
}

// Let the initial HTML paint before requesting the full count or probing tools.
requestAnimationFrame(() => setTimeout(() => {
  loadCatalog().catch((error) => {
    if (hasInitialCatalog) logLocal(error.message, "warn");
    else $("catalog-status").textContent = error.message;
  });
  loadEnvironment().catch((error) => logLocal(error.message, "error"));
  pollBatch();
  loadUploadLog().catch((error) => logLocal(error.message, "warn"));
}, 0));
