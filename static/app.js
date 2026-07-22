const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  view: "overview",
  jobs: [],
  results: [],
  health: null,
  settings: {},
  poolCount: 0,
  poolSummary: {total: 0, spare: 0, reserved: 0, registered: 0, failed: 0, alive: 0},
  poolAccounts: [],
  poolState: "",
  statusPoll: null,
  statusPollEditing: false,
  jobFilter: "all",
  resultFilter: "all",
  drawerJobId: null,
  drawerTab: "log",
  pollTimer: null,
};

const viewNames = {overview: "概览", launch: "新建任务", pool: "账号池", jobs: "任务队列", results: "注册结果", settings: "运行配置"};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"}[char]));
}

async function api(path, options = {}) {
  const config = {...options, headers: {...(options.headers || {})}};
  if (config.body && typeof config.body !== "string") {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(path, config);
  let payload;
  try { payload = await response.json(); }
  catch { payload = {ok: false, error: await response.text()}; }
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function toast(message, type = "ok") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.textContent = message;
  $("#toastStack").append(node);
  setTimeout(() => node.remove(), 3600);
}

function routeTo(view) {
  if (!viewNames[view]) view = "overview";
  state.view = view;
  $$(".view").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  $$("[data-nav]").forEach(node => node.classList.toggle("active", node.classList.contains("nav-item") && node.dataset.nav === view));
  $("#pageCrumb").textContent = viewNames[view];
  $("#sidebar").classList.remove("open");
  history.replaceState(null, "", `#${view}`);
  window.scrollTo({top: 0, behavior: "smooth"});
  if (view === "launch") loadPool();
  if (view === "jobs") loadJobs();
  if (view === "results") loadResults();
  if (view === "pool") { loadPoolAccounts(); loadStatusPoll(); }
  if (view === "settings") { loadSettings(); renderRuntime(); }
}

function displayTimezone() {
  return state.settings.display_timezone || state.health?.timezone || "Asia/Shanghai";
}

function formatDateTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: displayTimezone(), year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(date).replace(/\//g, "-");
}

function timeAgo(iso) {
  if (!iso) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return formatDateTime(iso);
}

function duration(seconds) {
  seconds = Number(seconds || 0);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function survivalDuration(seconds, fallback = "—") {
  if (seconds === null || seconds === undefined || seconds === "") return fallback;
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return fallback;
  if (value < 60) return `${Math.floor(value)} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分`;
  if (value < 86400) return `${Math.floor(value / 3600)} 时 ${Math.floor((value % 3600) / 60)} 分`;
  return `${Math.floor(value / 86400)} 天 ${Math.floor((value % 86400) / 3600)} 时`;
}

function statusLabel(value) {
  return ({running: "运行中", completed: "已完成", failed: "失败", stopped: "已停止", interrupted: "已中断", queued: "排队中"})[value] || value || "—";
}

function resultStatusLabel(value) {
  return ({agent_ready: "Agent Ready", phone_bound: "Phone Bound", registered: "Registered", phone_failed: "Phone Failed", agent_failed: "Agent Failed", failed: "Failed"})[value] || value || "—";
}

function statusClass(value) {
  if (["failed", "stopped", "interrupted", "phone_failed", "agent_failed"].includes(value)) return "failed";
  if (value === "running") return "running";
  if (value === "queued") return "queued";
  return "completed";
}

async function loadHealth() {
  try {
    state.health = await api("/api/health");
    renderHealth();
  } catch (error) {
    state.health = {ok: false, checks: {api: false}, error: error.message};
    renderHealth();
  }
}

function renderHealth() {
  const health = state.health || {ok: false, checks: {}};
  const checks = health.checks || {};
  const values = Object.values(checks);
  const score = values.length ? Math.round(values.filter(Boolean).length * 100 / values.length) : 0;
  $("#sideHealthDot").className = health.ok ? "ok" : "warn";
  $("#sideHealthText").textContent = health.ok ? "服务就绪" : "运行时需检查";
  $("#sideHealthSub").textContent = health.dry_run ? "DRY RUN MODE" : "CORE / API";
  $("#healthPill").className = `status-pill ${health.ok ? "" : "failed"}`;
  $("#healthPill").textContent = health.ok ? "READY" : "ATTENTION";
  $("#healthScore").textContent = score;
  $("#scoreRing").style.setProperty("--score", `${score * 3.6}deg`);
  $("#healthTitle").textContent = health.ok ? "独立运行时已就绪" : "有检查项未通过";
  $("#healthMessage").textContent = health.dry_run ? "当前使用演示运行器，用于界面和本地流程验收。" : "注册核心、依赖和输出目录均由本地应用管理。";
  const labels = {runner: "Free runner", runtime: "Protocol core", writable: "Output writable", curl_cffi: "curl_cffi", httpx: "httpx", cryptography: "cryptography", api: "Console API"};
  $("#healthChecks").innerHTML = Object.entries(checks).map(([key, ok]) => `<div class="check-row"><span>${escapeHtml(labels[key] || key)}</span><b class="${ok ? "ok" : "fail"}">${ok ? "PASS" : "MISSING"}</b></div>`).join("");
  renderRuntime();
}

async function loadPool() {
  try {
    const payload = await api("/api/accounts/pool");
    state.poolCount = payload.count || 0;
    state.poolSummary = {...state.poolSummary, ...(payload.summary || {})};
    const spare = Number(state.poolSummary.spare || 0);
    $("#poolCountText").textContent = `${spare} 个待注册备用账号`;
    $("#poolSelectionTitle").textContent = `从备用池领取本批账号 · ${spare} 个待注册`;
    $("#navPoolCount").textContent = spare;
    updateComposer();
  } catch {
    $("#poolCountText").textContent = "账号池读取异常";
  }
}

function poolStateLabel(value) {
  return ({spare: "待注册", reserved: "任务占用", registered: "已注册", failed: "注册失败"})[value] || value || "—";
}

function healthStatusLabel(value) {
  return ({free: "Free · 已确认", plus: "Plus · 已确认", k12: "K12 · 已确认", plus_expired: "订阅已过期 · 账号可用", account_deactivated: "已停用", token_dead: "令牌失效 · 待复核", rt_revoked: "RT 已撤销", protocol_login_failed: "登录检测失败", browser_login_failed: "登录检测失败", unknown_no_creds: "凭据不足", error: "探测未确认", unchecked: "未检测"})[value] || value || "未检测";
}

function healthStatusClass(value, alive) {
  if (value === "plus_expired") return "warning";
  if (alive === true || ["free", "plus", "k12"].includes(value)) return "completed";
  if (alive === false || value === "account_deactivated") return "failed";
  return "neutral";
}

function probeStatusLabel(value) {
  return ({free: "本次确认 Free", plus: "本次确认 Plus", k12: "本次确认 K12", plus_expired: "本次确认，订阅已过期", account_deactivated: "本次确认已停用", rt_revoked: "本次 RT 已撤销", no_token: "本次无可用凭据", token_dead: "本次 Token 失效", protocol_login_failed: "本次协议登录异常", browser_login_failed: "本次登录异常", unknown_no_creds: "本次凭据不足", error: "本次探测异常"})[value] || value || "";
}

function probeStatusClass(value) {
  if (["free", "plus", "k12"].includes(value)) return "completed";
  if (value === "account_deactivated") return "failed";
  if (!value) return "neutral";
  return "warning";
}

function healthProbeNote(row) {
  const probe = String(row.last_probe_status || "").toLowerCase();
  const health = String(row.health_status || "").toLowerCase();
  if (!probe || probe === health) return "";
  const detail = String(row.check_error || row.health_error || "");
  const title = detail ? ` title="${escapeHtml(detail)}"` : "";
  return `<small class="probe-note ${probeStatusClass(probe)}"${title}>${escapeHtml(probeStatusLabel(probe))}</small>`;
}

function confirmationLabel(row) {
  return row.last_confirmed_at ? `确认 ${formatDateTime(row.last_confirmed_at)}` : "尚未确认";
}

function latestProbeAlive(summary) {
  if (summary.probe_alive !== undefined) return Number(summary.probe_alive || 0);
  return Number(summary.free || 0) + Number(summary.plus || 0) + Number(summary.k12 || 0) + Number(summary.plus_expired || 0);
}

function latestProbeSummary(summary) {
  const parts = [`本次确认可用 ${latestProbeAlive(summary)} 个`];
  const inconclusive = Number(summary.probe_inconclusive || 0);
  const deactivated = Number(summary.probe_deactivated || 0);
  if (inconclusive) parts.push(`待复核 ${inconclusive} 个`);
  if (deactivated) parts.push(`明确停用 ${deactivated} 个`);
  return parts.join("，");
}

async function loadPoolAccounts() {
  try {
    const search = $("#poolSearch")?.value.trim() || "";
    const query = new URLSearchParams({limit: "500"});
    if (state.poolState) query.set("state", state.poolState);
    if (search) query.set("q", search);
    const payload = await api(`/api/accounts?${query}`);
    state.poolAccounts = payload.accounts || [];
    state.poolSummary = {...state.poolSummary, ...(payload.summary || {})};
    renderPool();
  } catch (error) { toast(error.message, "error"); }
}

function statusPollClass(poll) {
  if (poll?.running) return "running";
  if (poll?.enabled) return "completed";
  return "neutral";
}

function statusPollStateLabel(poll) {
  if (poll?.running) return "轮询中";
  return poll?.enabled ? "已启用" : "已暂停";
}

function statusPollValue(payload) {
  if (payload && typeof payload.status_poll === "object") return payload.status_poll;
  if (payload && typeof payload.poller === "object") return payload.poller;
  if (payload && typeof payload.poll === "object") return payload.poll;
  if (payload && typeof payload.status === "object") return payload.status;
  return payload || null;
}

function renderStatusPoll() {
  const pill = $("#statusPollPill");
  const detail = $("#statusPollStatus");
  const next = $("#statusPollNext");
  if (!pill || !detail || !next) return;
  const poll = state.statusPoll;
  if (!poll) {
    pill.className = "status-pill failed";
    pill.textContent = "读取失败";
    detail.textContent = "自动轮询状态暂时不可用。";
    next.textContent = "—";
    return;
  }

  if (!state.statusPollEditing) {
    $("#statusPollEnabled").checked = Boolean(poll.enabled);
    $("#statusPollInterval").value = String(poll.interval_minutes ?? 60);
    $("#statusPollConcurrency").value = String(poll.concurrency ?? 4);
    $("#statusPollRefreshRt").checked = poll.refresh_codex_rt !== false;
  }
  pill.className = `status-pill ${statusPollClass(poll)}`;
  pill.textContent = statusPollStateLabel(poll);

  const summary = poll.last_summary || {};
  const parts = ["仅使用 Codex RT 和现有 AT，不走协议登录"];
  if (summary.total !== undefined) {
    parts.push(`最近检查 ${Number(summary.total || 0)} 个 · Free ${Number(summary.free || 0)} · Plus ${Number(summary.plus || 0)} · 异常 ${Number(summary.errors || 0)}`);
  }
  if (Number(summary.skipped_without_token || 0)) {
    parts.push(`跳过无 RT/AT ${Number(summary.skipped_without_token)} 个`);
  }
  if (poll.last_error) parts.push(`异常：${poll.last_error}`);
  detail.textContent = parts.join(" · ");
  if (poll.running) next.textContent = "正在执行本轮检测";
  else if (poll.next_run_at) next.textContent = `下次 ${formatDateTime(poll.next_run_at)}`;
  else if (poll.last_finished_at || poll.persisted_checked_at) next.textContent = `上次 ${formatDateTime(poll.last_finished_at || poll.persisted_checked_at)}`;
  else next.textContent = poll.enabled ? "等待首次执行" : "轮询已暂停";
}

async function loadStatusPoll() {
  try {
    state.statusPoll = statusPollValue(await api("/api/accounts/status-poll"));
  } catch {
    state.statusPoll = null;
  }
  renderStatusPoll();
}

function boundedStatusPollNumber(node, minimum, maximum, fallback) {
  const number = Number(node?.value);
  return Number.isInteger(number) ? Math.max(minimum, Math.min(maximum, number)) : fallback;
}

async function saveStatusPoll() {
  const button = $("#saveStatusPoll");
  button.disabled = true;
  try {
    const payload = await api("/api/accounts/status-poll", {
      method: "PATCH",
      body: {
        enabled: $("#statusPollEnabled").checked,
        interval_minutes: boundedStatusPollNumber($("#statusPollInterval"), 15, 1440, 60),
        concurrency: boundedStatusPollNumber($("#statusPollConcurrency"), 1, 8, 4),
        refresh_codex_rt: $("#statusPollRefreshRt").checked,
      },
    });
    state.statusPoll = statusPollValue(payload);
    state.statusPollEditing = false;
    renderStatusPoll();
    toast("自动轮询配置已保存");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function runStatusPoll() {
  const button = $("#runStatusPoll");
  button.disabled = true;
  button.textContent = "正在排队…";
  try {
    const payload = await api("/api/accounts/status-poll/run", {method: "POST"});
    state.statusPoll = statusPollValue(payload);
    state.statusPollEditing = false;
    renderStatusPoll();
    toast(payload.queued === false ? "自动轮询正在运行" : "自动轮询已加入后台队列");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "立即轮询";
  }
}

function renderPool() {
  const summary = state.poolSummary || {};
  $("#poolSpare").textContent = Number(summary.spare || 0);
  $("#poolReserved").textContent = Number(summary.reserved || 0);
  $("#poolRegistered").textContent = Number(summary.registered || 0);
  $("#poolAlive").textContent = Number(summary.alive || 0);
  $("#navPoolCount").textContent = Number(summary.spare || 0);
  const rows = state.poolAccounts;
  $("#poolTableBody").innerHTML = rows.map(row => {
    const health = healthStatusLabel(row.health_status);
    const healthClass = healthStatusClass(row.health_status, row.health_alive);
    const probeNote = healthProbeNote(row);
    return `<tr>
      <td><div class="account-cell"><b>${escapeHtml(row.email)}</b><small>${escapeHtml(row.source_file || "账号池")}</small></div></td>
      <td><code>${escapeHtml(row.kind || "—")}</code></td>
      <td><span class="status-pill ${statusClass(row.state)}">${escapeHtml(poolStateLabel(row.state))}</span></td>
      <td><span>${formatDateTime(row.registered_at)}</span></td>
      <td><code>${survivalDuration(row.observed_seconds, row.registered_at ? "待确认" : "—")}</code></td>
      <td><div class="health-cell"><span class="status-pill ${healthClass}">${escapeHtml(health)}</span>${probeNote}</div></td>
      <td><div class="check-cell"><b>${formatDateTime(row.last_checked_at)}</b><small>${escapeHtml(confirmationLabel(row))}</small></div></td>
    </tr>`;
  }).join("");
  $("#poolEmpty").classList.toggle("hidden", rows.length > 0);
  $("#poolTableBody").closest(".table-wrap").classList.toggle("hidden", rows.length === 0);
}

async function loadJobs() {
  try {
    const payload = await api("/api/jobs");
    state.jobs = payload.jobs || [];
    renderJobs();
    renderOverview();
  } catch (error) { toast(error.message, "error"); }
}

function renderOverview() {
  const jobs = state.jobs;
  const running = jobs.filter(job => job.state === "running");
  const realJobs = jobs.filter(job => !job.dry_run);
  const success = realJobs.reduce((sum, job) => sum + Number(job.success_count || 0), 0);
  const failed = realJobs.reduce((sum, job) => sum + Number(job.failed_count || 0), 0);
  $("#metricRunning").textContent = running.length;
  $("#metricRunningSub").textContent = running.length ? `${running.reduce((sum, job) => sum + Number(job.workers || 0), 0)} 个并发执行槽正在工作` : "当前没有任务占用执行槽";
  $("#metricSuccess").textContent = success;
  $("#metricFailed").textContent = failed;
  $("#metricRate").textContent = success + failed ? `${Math.round(success * 100 / (success + failed))}%` : "—";
  $("#navRunningCount").textContent = running.length;

  const host = $("#overviewJobs");
  const rows = jobs.slice(0, 5);
  if (!rows.length) {
    host.innerHTML = `<div class="blank-list"><span>＋</span><b>等待第一批任务</b><p>从右上角新建一个注册任务。</p></div>`;
    return;
  }
  host.innerHTML = rows.map(job => `
    <div class="activity-row" data-job-id="${escapeHtml(job.id)}">
      <i class="${statusClass(job.state)}"></i>
      <div class="activity-name"><b>${escapeHtml(job.label)}</b><small>${escapeHtml(job.method)} · ${escapeHtml(job.proxy_label)} · ${job.account_count} accounts${job.dry_run ? " · 演示任务" : ""}</small></div>
      <div class="mini-progress"><div><i style="width:${job.progress}%"></i></div><span>${job.progress}%</span></div>
      <time>${timeAgo(job.created_at)}</time>
    </div>`).join("");
}

function renderJobs() {
  const search = ($("#jobSearch")?.value || "").toLowerCase();
  const rows = state.jobs.filter(job => {
    const filterOk = state.jobFilter === "all" || (state.jobFilter === "failed" ? ["failed", "interrupted", "stopped"].includes(job.state) : job.state === state.jobFilter);
    return filterOk && (!search || `${job.label} ${job.id}`.toLowerCase().includes(search));
  });
  const body = $("#jobsTableBody");
  body.innerHTML = rows.map(job => `
    <tr data-job-id="${escapeHtml(job.id)}">
      <td><div class="task-cell"><b>${escapeHtml(job.label)}</b><code>${escapeHtml(job.id)}</code></div></td>
      <td><span class="status-pill neutral">${escapeHtml(job.method)}${job.protocol_engine ? ` / ${escapeHtml(job.protocol_engine)}` : ""}${job.dry_run ? " · 演示" : ""}</span></td>
      <td><div class="table-progress"><div><i style="width:${job.progress}%"></i></div><span>${job.completed_count}/${job.account_count} · ${job.progress}%</span></div></td>
      <td><div class="result-counts"><b>✓ ${job.success_count}</b><em>× ${job.failed_count}</em></div></td>
      <td><code>${duration(job.duration_seconds)}</code></td>
      <td><span>${timeAgo(job.created_at)}</span></td>
      <td><button class="row-action" data-open-job="${escapeHtml(job.id)}">→</button></td>
    </tr>`).join("");
  $("#jobsEmpty").classList.toggle("hidden", rows.length > 0);
  $("#jobsTableBody").closest(".table-wrap").classList.toggle("hidden", rows.length === 0);
}

async function loadResults() {
  try {
    const payload = await api("/api/results");
    state.results = payload.results || [];
    renderResults();
  } catch (error) { toast(error.message, "error"); }
}

function renderResults() {
  const all = state.results;
  const realResults = all.filter(row => !row.dry_run);
  $("#resultTotal").textContent = realResults.length;
  $("#resultAgent").textContent = realResults.filter(row => row.status === "agent_ready").length;
  $("#resultPhone").textContent = realResults.filter(row => row.status === "phone_bound").length;
  $("#resultErrors").textContent = realResults.filter(row => !row.ok).length;
  const search = ($("#resultSearch")?.value || "").toLowerCase();
  const rows = all.filter(row => {
    const filterOk = state.resultFilter === "all" || (state.resultFilter === "failed" ? !row.ok : row.status === state.resultFilter);
    return filterOk && (!search || String(row.email).toLowerCase().includes(search));
  });
  $("#resultsTableBody").innerHTML = rows.map(row => {
    const trial = row.trial_eligible === true ? `<span class="trial-tag yes">ELIGIBLE</span>` : row.trial_eligible === false ? `<span class="trial-tag">NO OFFER</span>` : `<span class="trial-tag">${escapeHtml(row.trial_status || "UNKNOWN")}</span>`;
    const health = healthStatusLabel(row.health_status);
    const healthClass = healthStatusClass(row.health_status, row.health_alive);
    const probeNote = healthProbeNote(row);
    return `<tr>
      <td><div class="account-cell"><b>${escapeHtml(row.email)}</b><small>${formatDateTime(row.registered_at)}</small></div></td>
      <td><span class="status-pill ${statusClass(row.status)}">${escapeHtml(resultStatusLabel(row.status))}</span>${row.dry_run ? " <span class=\"status-pill neutral\">演示</span>" : ""}</td>
      <td>${escapeHtml(row.method)}${row.protocol_engine ? ` / ${escapeHtml(row.protocol_engine)}` : ""}</td>
      <td>${trial}</td>
      <td><div class="health-cell"><span class="status-pill ${healthClass}">${escapeHtml(health)}</span>${probeNote}</div></td>
      <td><code>${survivalDuration(row.observed_seconds, row.registered_at ? "待确认" : "—")}</code></td>
      <td><code>${escapeHtml(row.proxy_region || "—")}</code></td>
      <td><code>${(Number(row.duration_ms || 0) / 1000).toFixed(1)}s</code></td>
      <td><button class="text-button" data-open-job="${escapeHtml(row.job_id)}">${escapeHtml(row.job_id.slice(-10))} →</button></td>
    </tr>`;
  }).join("");
  $("#resultsEmpty").classList.toggle("hidden", rows.length > 0);
  $("#resultsTableBody").closest(".table-wrap").classList.toggle("hidden", rows.length === 0);
}

function poolFilterEmails() {
  const seen = new Set();
  $("#poolFilter").value.split(/\r?\n/).forEach(line => {
    const email = line.trim().split("----", 1)[0].trim().toLowerCase();
    if (email.includes("@")) seen.add(email);
  });
  return [...seen];
}

function accountInputCount() {
  const usePool = $("input[name=accountSource]:checked")?.value === "pool";
  if (usePool) {
    const filter = poolFilterEmails();
    const batchSize = Math.max(1, Number($("#poolBatchSize").value || 1));
    const includeFailed = $("#poolRetryFailed").checked;
    const available = Number((includeFailed ? Number(state.poolSummary.spare || 0) + Number(state.poolSummary.failed || 0) : state.poolSummary.spare) || 0);
    return filter.length ? Math.min(batchSize, filter.length) : Math.min(batchSize, available);
  }
  const seen = new Set();
  $("#accountsInput").value.split(/\r?\n/).forEach(line => {
    const value = line.trim();
    const email = value.split("----", 1)[0].toLowerCase();
    if (value && !value.startsWith("#") && email.includes("@")) seen.add(email);
  });
  return seen.size;
}

function updateComposer() {
  const method = $("input[name=method]:checked")?.value || "protocol";
  const source = $("input[name=accountSource]:checked")?.value || "paste";
  const proxyMode = $("input[name=proxyMode]:checked")?.value || "managed_jp";
  const postAction = $("input[name=postAction]:checked")?.value || "agent";
  const smsSource = $("input[name=smsSource]:checked")?.value || "platform";
  const count = accountInputCount();
  const poolFilter = poolFilterEmails();
  const poolBatch = Math.max(1, Number($("#poolBatchSize").value || 1));
  const retryFailed = $("#poolRetryFailed").checked;
  const poolAvailable = Number((retryFailed ? Number(state.poolSummary.spare || 0) + Number(state.poolSummary.failed || 0) : state.poolSummary.spare) || 0);
  $("#accountPastePanel").classList.toggle("hidden", source !== "paste");
  $("#accountPoolPanel").classList.toggle("hidden", source !== "pool");
  $("#poolBatchNote").textContent = poolFilter.length
    ? `已指定 ${poolFilter.length} 个账号；本批最多领取 ${Math.min(poolBatch, poolFilter.length)} 个。`
    : `本批将领取 ${Math.min(poolBatch, poolAvailable)} / ${poolAvailable} 个${retryFailed ? "待注册或失败" : "待注册"}账号。`;
  $("#accountCount").textContent = `${count} 个账号`;
  $("#protocolOptions").classList.toggle("hidden", method !== "protocol");
  $("#browserOptions").classList.toggle("hidden", method !== "browser");
  if (method === "browser" && postAction === "agent") {
    $("input[name=postAction][value=none]").checked = true;
    return updateComposer();
  }
  $("#managedProxyPanel").classList.toggle("hidden", !proxyMode.startsWith("managed_"));
  $("#singleProxyPanel").classList.toggle("hidden", proxyMode !== "single");
  $("#poolProxyPanel").classList.toggle("hidden", proxyMode !== "pool");
  $("#managedProxyTitle").textContent = `${proxyMode === "managed_us" ? "United States" : "Japan"} · 100 sessions`;
  $("#agentOptions").classList.toggle("hidden", postAction !== "agent");
  $("#phoneOptions").classList.toggle("hidden", postAction !== "phone");
  $("#manualPhonePanel").classList.toggle("hidden", !(postAction === "phone" && smsSource === "manual"));
  $("#summaryAccounts").textContent = count;
  $("#summaryMethod").textContent = method === "protocol" ? "Protocol / Mail Auth" : `Browser / ${$("#browserChoice").value}`;
  $("#summaryWorkers").textContent = method === "protocol" ? $("#workers").value : $("#browserWorkers").value;
  $("#summaryProxy").textContent = ({managed_jp: "JP · 100", managed_us: "US · 100", single: "单代理", pool: "自定义池"})[proxyMode];
  $("#summaryCredential").textContent = ({agent: "Agent Identity", phone: smsSource === "platform" ? "Phone / Platform" : "Phone / Manual", none: "Free Token"})[postAction];
  $("#ticketStatusDot").classList.toggle("ready", count > 0);
  const managedProxyReady = Boolean(
    String(state.settings.proxy_host || "").trim() &&
    String(state.settings.proxy_port || "").trim() &&
    String(state.settings.proxy_user || "").trim()
  );
  const proxyReady = proxyMode.startsWith("managed_")
    ? managedProxyReady
    : proxyMode === "single"
      ? Boolean($("#singleProxy").value.trim())
      : Boolean($("#proxyPool").value.trim());
  $("#preflightList").innerHTML = `
    <li class="${count ? "ok" : "pending"}"><i></i>${count ? `${count} 个账号已识别` : "等待账号输入"}</li>
    <li class="ok"><i></i>${method === "protocol" ? "协议内核已选择" : "浏览器运行时已选择"}</li>
    <li class="${proxyReady ? "ok" : "error"}"><i></i>${proxyReady ? "网络出口已配置" : (proxyMode.startsWith("managed_") ? "托管代理缺少 host / port / user" : "网络出口待配置")}</li>`;
}

async function submitRun(event) {
  event.preventDefault();
  const button = $("#launchButton");
  const method = $("input[name=method]:checked").value;
  const source = $("input[name=accountSource]:checked").value;
  const post = $("input[name=postAction]:checked").value;
  const smsSource = $("input[name=smsSource]:checked")?.value || "platform";
  const proxyMode = $("input[name=proxyMode]:checked").value;
  if (proxyMode.startsWith("managed_") && !(
    String(state.settings.proxy_host || "").trim() &&
    String(state.settings.proxy_port || "").trim() &&
    String(state.settings.proxy_user || "").trim()
  )) {
    toast("请先在“运行配置 → 托管代理”填写并保存 host / port / user", "error");
    routeTo("settings");
    return;
  }
  const body = {
    name: $("#runName").value.trim(),
    accounts: source === "pool" ? $("#poolFilter").value : $("#accountsInput").value,
    use_pool: source === "pool",
    pool_batch_size: Number($("#poolBatchSize").value),
    pool_retry_failed: $("#poolRetryFailed").checked,
    method,
    protocol_engine: $("#protocolEngine").value,
    browser: $("#browserChoice").value,
    workers: Number(method === "protocol" ? $("#workers").value : $("#browserWorkers").value),
    otp_timeout: Number($("#otpTimeout").value),
    no_password: !$("#withPassword").checked,
    proxy_mode: proxyMode,
    proxy: $("#singleProxy").value.trim(),
    proxy_pool: $("#proxyPool").value.trim(),
    agent_identity: post === "agent",
    bind_phone: post === "phone",
    sms_source: post === "phone" ? smsSource : "none",
    phone_lines: $("#phoneLines").value,
    sms_workers: Number($("#smsWorkers").value),
    sms_otp_timeout: Number($("#smsOtpTimeout").value),
    sms_max_attempts: Number($("#smsAttempts").value),
    sms_max_otp_retries: Number($("#smsRetries").value),
    sub2api_export: post === "agent" && $("#sub2Export").checked,
    sub2api_import: post === "agent" && $("#sub2Import").checked,
  };
  button.disabled = true;
  button.querySelector("span").textContent = "正在提交…";
  try {
    const payload = await api("/api/runs", {method: "POST", body});
    toast(`任务已启动：${payload.job.label}`);
    await Promise.all([loadJobs(), loadPool(), loadPoolAccounts()]);
    openJob(payload.job.id);
    routeTo("jobs");
  } catch (error) {
    const missing = error.payload?.missing?.length ? `：${error.payload.missing.join(", ")}` : "";
    toast(`${error.message}${missing}`, "error");
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "启动注册任务";
  }
}

async function testProxy() {
  const mode = $("input[name=proxyMode]:checked").value;
  const button = $("#testProxy");
  button.textContent = "测试中…";
  try {
    const body = mode.startsWith("managed_") ? {managed_region: mode === "managed_jp" ? "JP" : "US"} : {proxy: mode === "single" ? $("#singleProxy").value : ($("#proxyPool").value.split(/\r?\n/).find(Boolean) || "").split("|").pop()};
    const result = await api("/api/proxy/test", {method: "POST", body});
    $("#proxyTestResult").className = "inline-result";
    $("#proxyTestResult").textContent = `✓ ${result.ip} · ${result.country_code} / ${result.city} · ${result.isp}`;
    $("#proxyTestBadge").textContent = result.country_code || "PASS";
    toast(`出口验证通过：${result.ip}`);
  } catch (error) {
    $("#proxyTestResult").className = "inline-result error";
    $("#proxyTestResult").textContent = `× ${error.message}`;
    $("#proxyTestBadge").textContent = "FAILED";
  } finally { button.textContent = "测试出口"; }
}

async function openJob(jobId) {
  state.drawerJobId = jobId;
  $("#drawerBackdrop").classList.remove("hidden");
  $("#jobDrawer").classList.add("open");
  $("#jobDrawer").setAttribute("aria-hidden", "false");
  await refreshDrawer();
}

function closeDrawer() {
  state.drawerJobId = null;
  $("#drawerBackdrop").classList.add("hidden");
  $("#jobDrawer").classList.remove("open");
  $("#jobDrawer").setAttribute("aria-hidden", "true");
}

async function refreshDrawer() {
  if (!state.drawerJobId) return;
  try {
    const payload = await api(`/api/jobs/${encodeURIComponent(state.drawerJobId)}?tail=800`);
    const job = payload.job;
    $("#drawerTitle").textContent = job.label;
    $("#drawerId").textContent = job.id;
    $("#drawerState").className = `status-pill ${statusClass(job.state)}`;
    $("#drawerState").textContent = job.dry_run ? "演示结果" : statusLabel(job.state);
    $("#drawerProgressText").textContent = `${job.progress}%`;
    $("#drawerProgressBar").style.width = `${job.progress}%`;
    $("#drawerSuccess").textContent = job.success_count;
    $("#drawerFailed").textContent = job.failed_count;
    $("#drawerDone").textContent = `${job.completed_count}/${job.account_count}`;
    $("#drawerDuration").textContent = duration(job.duration_seconds);
    $("#drawerLog").textContent = (job.logs || []).join("\n") || "等待运行日志…";
    if (job.state === "running" && state.drawerTab === "log") $(".drawer-body").scrollTop = $(".drawer-body").scrollHeight;
    $("#drawerResults").innerHTML = (job.results || []).map(row => `<div class="drawer-result-row"><div><b>${escapeHtml(row.email)}</b><small>${escapeHtml(row.error || `${row.method} · ${row.protocol_engine || row.status}`)}</small></div><span class="status-pill ${statusClass(row.status)}">${escapeHtml(resultStatusLabel(row.status))}</span></div>`).join("") || `<div class="blank-list"><span>…</span><b>结果写入后显示</b><p>注册核心按账号追加 JSONL。</p></div>`;
    $("#downloadLog").href = `/api/jobs/${encodeURIComponent(job.id)}/download/log`;
    $("#downloadResults").href = `/api/jobs/${encodeURIComponent(job.id)}/download/results`;
    $("#stopJob").classList.toggle("hidden", job.state !== "running");
  } catch (error) { toast(error.message, "error"); }
}

async function stopCurrentJob() {
  if (!state.drawerJobId || !confirm("停止当前任务？已写入的账号结果会保留。")) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(state.drawerJobId)}/stop`, {method: "POST"});
    toast("已发送停止信号");
    setTimeout(refreshDrawer, 500);
  } catch (error) { toast(error.message, "error"); }
}

async function loadSettings() {
  try {
    const payload = await api("/api/settings");
    state.settings = payload.settings || {};
    $$('[data-setting]').forEach(input => input.value = state.settings[input.dataset.setting] ?? "");
    for (const [key, configured] of Object.entries(payload.secret_configured || {})) {
      const input = $(`[data-setting="${key}"]`);
      if (input && configured) input.placeholder = "•••••••• · 已配置，留空保留";
    }
    updateComposer();
    updateLiveClock();
    if (state.results.length) renderResults();
    if (state.poolAccounts.length) renderPool();
  } catch (error) { toast(error.message, "error"); }
}

async function saveSettings() {
  const body = {};
  $$('[data-setting]').forEach(input => body[input.dataset.setting] = input.value);
  try {
    await api("/api/settings", {method: "PATCH", body});
    toast("运行配置已保存");
    await loadSettings();
  } catch (error) { toast(error.message, "error"); }
}

function renderRuntime() {
  if (!$("#runtimeDetails")) return;
  const checks = state.health?.checks || {};
  const labels = {runner: "Free runner", runtime: "Protocol core", writable: "Output directory", curl_cffi: "curl_cffi", httpx: "HTTPX", cryptography: "Cryptography"};
  $("#runtimeDetails").innerHTML = Object.entries(checks).map(([key, ok]) => `<div class="runtime-card"><span>${escapeHtml(labels[key] || key)}</span><b class="${ok ? "" : "fail"}">${ok ? "READY" : "CHECK REQUIRED"}</b></div>`).join("");
}

async function testSms() {
  const node = $("#smsTestResult");
  node.className = "inline-result";
  node.textContent = "正在读取 Provider 余额…";
  try {
    const result = await api("/api/sms/test", {method: "POST"});
    node.textContent = `✓ ${result.provider} · balance ${result.balance ?? "—"}`;
  } catch (error) {
    node.className = "inline-result error";
    node.textContent = `× ${error.message}`;
  }
}

async function importAccounts() {
  const button = $("#importAccounts");
  button.disabled = true;
  try {
    const result = await api("/api/accounts/import", {method: "POST", body: {lines: $("#importLines").value, mode: $("#importMode").value}});
    toast(`已导入 ${result.imported} 个账号`);
    $("#importModal").classList.add("hidden");
    $("#importLines").value = "";
    await Promise.all([loadPool(), loadPoolAccounts()]);
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; }
}

function updateLiveClock() {
  const node = $("#liveClock");
  if (!node) return;
  node.textContent = new Intl.DateTimeFormat("zh-CN", {
    timeZone: displayTimezone(), hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(new Date());
  node.title = displayTimezone();
}

function bindEvents() {
  $$('[data-nav]').forEach(node => node.addEventListener("click", event => { event.preventDefault(); routeTo(node.dataset.nav); }));
  $("#mobileMenu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#refreshButton").addEventListener("click", async () => { await Promise.all([loadHealth(), loadPool(), loadPoolAccounts(), loadStatusPoll(), loadJobs(), loadResults()]); toast("数据已刷新"); });
  $("#refreshJobs").addEventListener("click", loadJobs);
  $("#refreshResults").addEventListener("click", loadResults);
  $("#jobSearch").addEventListener("input", renderJobs);
  $("#resultSearch").addEventListener("input", renderResults);
  $("#jobFilters").addEventListener("click", event => { const button = event.target.closest("button[data-filter]"); if (!button) return; state.jobFilter = button.dataset.filter; $$("button", event.currentTarget).forEach(b => b.classList.toggle("active", b === button)); renderJobs(); });
  $("#resultFilters").addEventListener("click", event => { const button = event.target.closest("button[data-filter]"); if (!button) return; state.resultFilter = button.dataset.filter; $$("button", event.currentTarget).forEach(b => b.classList.toggle("active", b === button)); renderResults(); });
  $("#poolFilters").addEventListener("click", event => { const button = event.target.closest("button[data-pool-state]"); if (!button) return; state.poolState = button.dataset.poolState; $$("button", event.currentTarget).forEach(b => b.classList.toggle("active", b === button)); loadPoolAccounts(); });
  $("#poolSearch").addEventListener("input", loadPoolAccounts);
  document.addEventListener("click", event => { const button = event.target.closest("[data-open-job]"); const row = event.target.closest(".activity-row[data-job-id]"); if (button) openJob(button.dataset.openJob); else if (row) openJob(row.dataset.jobId); });

  $("#launchForm").addEventListener("submit", submitRun);
  $$("#launchForm input, #launchForm select, #launchForm textarea").forEach(node => node.addEventListener("input", updateComposer));
  $$("#launchForm input[type=radio]").forEach(node => node.addEventListener("change", updateComposer));
  $("#testProxy").addEventListener("click", testProxy);
  $("#poolImportButton").addEventListener("click", () => $("#importModal").classList.remove("hidden"));
  $("#poolLaunchButton").addEventListener("click", () => { $("input[name=accountSource][value=pool]").checked = true; updateComposer(); routeTo("launch"); });
  $("#saveStatusPoll").addEventListener("click", saveStatusPoll);
  $("#runStatusPoll").addEventListener("click", runStatusPoll);
  ["#statusPollEnabled", "#statusPollInterval", "#statusPollConcurrency", "#statusPollRefreshRt"].forEach(selector => {
    const node = $(selector);
    if (!node) return;
    node.addEventListener("input", () => { state.statusPollEditing = true; });
    node.addEventListener("change", () => { state.statusPollEditing = true; });
  });
  $("#closeDrawer").addEventListener("click", closeDrawer);
  $("#drawerBackdrop").addEventListener("click", closeDrawer);
  $("#stopJob").addEventListener("click", stopCurrentJob);
  $(".drawer-tabs").addEventListener("click", event => { const button = event.target.closest("button"); if (!button) return; state.drawerTab = button.dataset.drawerTab; $$("button", event.currentTarget).forEach(b => b.classList.toggle("active", b === button)); $("#drawerLog").classList.toggle("hidden", state.drawerTab !== "log"); $("#drawerResults").classList.toggle("hidden", state.drawerTab !== "results"); });

  $("#saveSettings").addEventListener("click", saveSettings);
  $("#testSms").addEventListener("click", testSms);
  $("#runtimeRefresh").addEventListener("click", loadHealth);
  $(".settings-nav").addEventListener("click", event => { const button = event.target.closest("button[data-setting-panel]"); if (!button) return; $$("button", event.currentTarget).forEach(b => b.classList.toggle("active", b === button)); $$(".setting-panel").forEach(panel => panel.classList.toggle("active", panel.dataset.panel === button.dataset.settingPanel)); });
  $("#openImport").addEventListener("click", () => $("#importModal").classList.remove("hidden"));
  $("#closeImport").addEventListener("click", () => $("#importModal").classList.add("hidden"));
  $("#importModal").addEventListener("click", event => { if (event.target === event.currentTarget) event.currentTarget.classList.add("hidden"); });
  $("#importAccounts").addEventListener("click", importAccounts);
  document.addEventListener("keydown", event => { if (event.key === "Escape") { closeDrawer(); $("#importModal").classList.add("hidden"); } if (event.key.toLowerCase() === "n" && !/input|textarea|select/i.test(document.activeElement.tagName)) routeTo("launch"); });
}

async function init() {
  bindEvents();
  updateLiveClock();
  setInterval(updateLiveClock, 1000);
  routeTo(location.hash.slice(1) || "overview");
  updateComposer();
  await Promise.all([loadHealth(), loadPool(), loadPoolAccounts(), loadStatusPoll(), loadJobs(), loadResults(), loadSettings()]);
  state.pollTimer = setInterval(async () => {
    const statusPollRunning = Boolean(state.statusPoll?.running);
    if (state.jobs.some(job => job.state === "running") || state.drawerJobId || statusPollRunning || state.view === "pool") {
      await loadJobs();
      if (state.drawerJobId) await refreshDrawer();
      if (state.view === "results") await loadResults();
      if (state.view === "pool" || state.view === "launch" || statusPollRunning) {
        await Promise.all([loadPool(), loadPoolAccounts(), loadStatusPoll()]);
      }
    }
  }, 2200);
}

init();
