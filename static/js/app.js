const state = {
  view: window.location.pathname === "/dashboard"
    ? "dashboard"
    : window.location.pathname === "/admin/credits" ? "admin-credits" : window.location.pathname === "/wallet" ? "wallet" : "single",
  mode: "paste",
  fileEmails: [],
  user: null,
  authMode: "login",
  jobId: sessionStorage.getItem("verigo_job_id"),
  guestToken: sessionStorage.getItem("verigo_job_token"),
  pollTimer: null,
  results: [],
  resultsAvailable: 0,
  page: 0,
  downloadName: null,
  discovery: { jobId: null, candidates: [], results: [] },
  metricsTimer: null,
  turnstileSiteKey: "",
  turnstileWidgetId: null,
  notifications: [],
  notificationTimer: null,
};

const pageSize = 50;

const el = (id) => document.getElementById(id);
const batchInput = el("email-input");
const singleInput = el("single-email-input");
const count = el("email-count");
const startButton = el("start-button");
const errorBox = el("form-error");
const statusLabels = { queued: "排队中", running: "验证中", completed: "已完成", failed: "失败", stopped: "已停止" };
const modeLabels = {
  1: ["稳定模式", "mode-stable"],
  2: ["标准模式", "mode-standard"],
  4: ["快速模式", "mode-fast"],
  8: ["极速模式", "mode-extreme"],
};

function splitEmails(text) {
  return text.split(/[\s,;，；]+/).map((value) => value.trim()).filter((value) => value.includes("@"));
}

function currentEmails() {
  if (state.view === "single") return splitEmails(singleInput.value);
  return state.mode === "file" ? state.fileEmails : splitEmails(batchInput.value);
}

function emailDomain(email) {
  return String(email).trim().toLowerCase().split("@").pop() || "";
}

function isQqEmail(email) {
  return ["qq.com", "vip.qq.com", "foxmail.com"].includes(emailDomain(email));
}

function isYahooEmail(email) {
  const domain = emailDomain(email);
  return domain.startsWith("yahoo.") || domain === "ymail.com" || domain === "rocketmail.com";
}

const yahooUnsupportedMessage = "暂不支持 Yahoo 邮箱验证（含所有国家或地区后缀，以及 ymail.com、rocketmail.com）。Yahoo 的反验证策略非常严格，当前全网常规验证均难以稳定通过，暂时没有可靠解决方案。";

function updateProviderNotice(emails) {
  const notice = el("qq-rate-notice");
  const hasQq = emails.some(isQqEmail);
  notice.classList.toggle("hidden", !hasQq);
  notice.textContent = hasQq
    ? "检测到 QQ 邮箱：将采用专属低并发与自动退避策略，验证速度会较慢，请耐心等待。"
    : "";
}

function updateCount() {
  const total = currentEmails().length;
  updateProviderNotice(currentEmails());
  count.textContent = total.toLocaleString();
  if (state.view === "single") {
    startButton.textContent = "免费验证";
  } else if (total > 0) {
    startButton.textContent = `开始验证 · ${total.toLocaleString()} 额度`;
  } else {
    startButton.textContent = "开始验证";
  }
}

function jobHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.guestToken) headers["X-Job-Token"] = state.guestToken;
  return headers;
}

async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: jobHeaders(options.headers || {}) });
  let body = null;
  try { body = await response.json(); } catch (_) { body = null; }
  if (!response.ok) {
    const detail = body?.detail;
    const message = Array.isArray(detail) ? detail.map((item) => item.msg).join("；") : detail;
    throw new Error(message || `请求失败 (${response.status})`);
  }
  return body;
}

function switchView(view) {
  const adminView = view === "dashboard" || view === "admin-credits";
  if (adminView && !state.user?.is_admin) {
    if (!state.user) {
      el("auth-dialog").showModal();
      setAuthMode("login");
      el("auth-error").textContent = "请先登录管理员账户";
    }
    return;
  }
  const discovery = view === "discovery";
  const dashboard = view === "dashboard";
  const adminCredits = view === "admin-credits";
  const wallet = view === "wallet";
  if (wallet && !state.user) { el("auth-dialog").showModal(); return; }
  if (discovery && !state.user) {
    el("auth-dialog").showModal();
    setAuthMode("login");
    el("auth-error").textContent = "请先登录后使用工作邮箱查找";
    return;
  }
  state.view = view;
  el("verify-workspace").classList.toggle("hidden", discovery || dashboard || adminCredits || wallet);
  el("discovery-workspace").classList.toggle("hidden", !discovery);
  el("dashboard-workspace").classList.toggle("hidden", !dashboard);
  el("admin-credits-workspace").classList.toggle("hidden", !adminCredits);
  el("wallet-workspace").classList.toggle("hidden", !wallet);
  el("single-panel").classList.toggle("hidden", view !== "single");
  el("batch-panel").classList.toggle("hidden", view !== "batch");
  if (!discovery && !dashboard && !adminCredits && !wallet) {
    el("verify-eyebrow").textContent = view === "single" ? "免费单个验证" : "收费批量验证";
    el("verify-heading").textContent = view === "single" ? "验证单个收件地址" : "批量验证收件地址";
  }
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  if (dashboard) {
    document.title = "运营监控 | Verigo";
    if (window.location.pathname !== "/dashboard") window.history.pushState({}, "", "/dashboard");
    loadDashboardMetrics();
    clearInterval(state.metricsTimer);
    state.metricsTimer = window.setInterval(loadDashboardMetrics, 30000);
  } else if (adminCredits) {
    document.title = "额度管理 | Verigo";
    if (window.location.pathname !== "/admin/credits") window.history.pushState({}, "", "/admin/credits");
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
  } else if (wallet) {
    document.title = "资金与使用 | Verigo";
    if (window.location.pathname !== "/wallet") window.history.pushState({}, "", "/wallet");
    loadWallet();
  } else {
    document.title = "Verigo";
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
    if (["/dashboard", "/admin/credits", "/wallet"].includes(window.location.pathname)) window.history.replaceState({}, "", "/");
  }
  updateCount();
}

function formatMoney(fen) {
  return `¥${(Number(fen || 0) / 100).toFixed(2)}`;
}

function setMetric(id, value) {
  el(id).textContent = Number(value || 0).toLocaleString("zh-CN");
}

function formatDuration(seconds) {
  const total = Math.round(Number(seconds || 0));
  if (total < 60) return `${total} 秒`;
  return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
}

function renderTraffic(days) {
  const chart = el("dashboard-traffic-chart");
  const width = 760;
  const height = 270;
  const padding = { top: 18, right: 16, bottom: 34, left: 38 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const series = [
    { key: "unique_visitors", color: "#1a73e8", label: "独立访客" },
    { key: "engaged_sessions", color: "#34a853", label: "互动会话" },
  ];
  const maximum = Math.max(1, ...days.flatMap((item) => series.map((itemSeries) => Number(item[itemSeries.key] || 0))));
  const point = (value, index) => {
    const x = padding.left + (days.length > 1 ? index * plotWidth / (days.length - 1) : plotWidth / 2);
    const y = padding.top + plotHeight - Number(value || 0) / maximum * plotHeight;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  };
  const grid = [0, 0.5, 1].map((step) => {
    const y = padding.top + plotHeight * step;
    const label = Math.round(maximum * (1 - step));
    return `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" class="traffic-grid" /><text x="0" y="${y + 4}" class="traffic-axis">${label}</text>`;
  }).join("");
  const labels = days.map((item, index) => {
    if (index % 2 && days.length > 8) return "";
    const x = padding.left + (days.length > 1 ? index * plotWidth / (days.length - 1) : plotWidth / 2);
    return `<text x="${x}" y="${height - 8}" text-anchor="middle" class="traffic-axis">${item.day.slice(5).replace("-", "/")}</text>`;
  }).join("");
  const lines = series.map((itemSeries) => {
    const points = days.map((item, index) => point(item[itemSeries.key], index)).join(" ");
    const dots = days.map((item, index) => {
      const [x, y] = point(item[itemSeries.key], index).split(",");
      return `<circle cx="${x}" cy="${y}" r="3" fill="${itemSeries.color}"><title>${item.day} ${itemSeries.label}：${Number(item[itemSeries.key] || 0).toLocaleString("zh-CN")}</title></circle>`;
    }).join("");
    return `<polyline points="${points}" fill="none" stroke="${itemSeries.color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />${dots}`;
  }).join("");
  chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  chart.innerHTML = `${grid}${lines}${labels}`;
}

async function loadDashboardMetrics() {
  if (!state.user?.is_admin || state.view !== "dashboard") return;
  try {
    const data = await api("/api/admin/metrics");
    const today = data.today;
    const realSessions = Math.max(0, Number(today.sessions || 0) - Number(today.suspected_bots || 0));
    const submissions = Number(today.free_submissions || 0) + Number(today.batch_submissions || 0);
    const engagementRate = realSessions ? Number(today.engaged_sessions || 0) / realSessions * 100 : 0;
    const submissionRate = realSessions ? submissions / realSessions * 100 : 0;
    setMetric("metric-report-users", today.unique_visitors);
    setMetric("metric-report-engaged", today.engaged_sessions);
    el("metric-report-engagement-rate").textContent = `互动率 ${engagementRate.toFixed(1)}%`;
    setMetric("metric-report-submissions", submissions);
    el("metric-report-submission-rate").textContent = `会话转化 ${submissionRate.toFixed(1)}%`;
    el("metric-report-engagement-time").textContent = formatDuration(today.average_engagement_seconds);
    setMetric("metric-today-visitors", data.today.unique_visitors);
    setMetric("metric-today-engaged", today.engaged_sessions);
    setMetric("metric-today-bots", today.suspected_bots);
    el("metric-today-bounce").textContent = `${Number(today.bounce_rate || 0).toFixed(1)}%`;
    el("metric-today-bot-rate").textContent = `${Number(today.bot_rate || 0).toFixed(1)}%`;
    el("metric-quality-human-rate").textContent = `${(100 - Number(today.bot_rate || 0)).toFixed(1)}%`;
    el("quality-ring").style.setProperty("--quality-human", `${Math.max(0, 100 - Number(today.bot_rate || 0))}%`);
    setMetric("metric-today-free-submissions", today.free_submissions);
    setMetric("metric-today-batch-submissions", today.batch_submissions);
    setMetric("metric-funnel-engaged", today.engaged_sessions);
    setMetric("metric-today-users", today.new_users);
    setMetric("metric-today-verified", today.verified_users);
    const userBase = Math.max(1, Number(today.unique_visitors || 0));
    [["funnel-users", today.unique_visitors], ["funnel-engaged", today.engaged_sessions], ["funnel-free", today.free_submissions], ["funnel-batch", today.batch_submissions]].forEach(([id, value]) => {
      el(id).style.width = `${Math.max(3, Number(value || 0) / userBase * 100)}%`;
    });
    el("metric-job-completion").textContent = `${Number(today.job_completion_rate || 0).toFixed(1)}%`;
    el("metric-job-duration").textContent = formatDuration(today.average_job_seconds);
    el("metric-deliverable-rate").textContent = `${Number(today.deliverable_rate || 0).toFixed(1)}%`;
    setMetric("metric-results-processed", today.results_processed);
    setMetric("metric-total-users", data.totals.users);
    setMetric("metric-total-verified-users", data.totals.verified_users);
    setMetric("metric-audience-visitors", today.unique_visitors);
    setMetric("metric-audience-engaged", today.engaged_sessions);
    setMetric("metric-audience-signups", today.new_users);
    setMetric("metric-audience-verified", today.verified_users);
    el("metric-audience-engagement-rate").textContent = `互动率 ${engagementRate.toFixed(1)}%`;
    el("metric-today-revenue").textContent = formatMoney(today.revenue_fen);
    el("metric-today-orders").textContent = `${Number(today.paid_orders || 0).toLocaleString("zh-CN")} 笔已支付订单`;
    el("metric-total-revenue").textContent = formatMoney(data.totals.revenue_fen);
    setMetric("metric-total-paid-orders", data.totals.paid_orders);
    const averageOrderFen = Number(data.totals.paid_orders || 0) ? Number(data.totals.revenue_fen || 0) / Number(data.totals.paid_orders) : 0;
    el("metric-average-order-value").textContent = formatMoney(averageOrderFen);
    ["queued", "running", "failed"].forEach((status) => setMetric(`metric-jobs-${status}`, data.jobs[status]));
    renderTraffic(data.daily);
    el("dashboard-updated").textContent = `最近更新：${new Date(data.updated_at).toLocaleString("zh-CN")}`;
  } catch (error) {
    el("dashboard-updated").textContent = `数据加载失败：${error.message}`;
  }
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});

document.querySelectorAll("[data-mode]").forEach((button) => {
  button.addEventListener("click", () => {
    state.mode = button.dataset.mode;
    document.querySelectorAll("[data-mode]").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", active ? "true" : "false");
    });
    el("paste-panel").classList.toggle("hidden", state.mode !== "paste");
    el("file-panel").classList.toggle("hidden", state.mode !== "file");
    updateCount();
  });
});

batchInput.addEventListener("input", updateCount);
singleInput.addEventListener("input", updateCount);
let engagementRecorded = false;
const analyticsStartedAt = performance.now();
function sendEngagement(seconds) {
  fetch("/api/analytics/engage", {
    method: "POST", credentials: "same-origin", keepalive: true,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ seconds }),
  }).catch(() => {});
}
function recordEngagement() {
  if (engagementRecorded) return;
  engagementRecorded = true;
  sendEngagement(Math.max(10, Math.round((performance.now() - analyticsStartedAt) / 1000)));
}
window.setTimeout(recordEngagement, 10000);
["pointerdown", "keydown", "scroll"].forEach((eventName) => {
  window.addEventListener(eventName, recordEngagement, { once: true, passive: true });
});
window.addEventListener("pagehide", () => {
  if (engagementRecorded) sendEngagement(Math.round((performance.now() - analyticsStartedAt) / 1000));
});

async function importFile(file) {
  state.fileEmails = [];
  if (!file) return updateCount();
  el("file-title").textContent = "正在解析…";
  el("file-meta").textContent = file.name;
  errorBox.textContent = "";
  const form = new FormData();
  form.append("file", file);
  try {
    const payload = await api("/api/import", { method: "POST", body: form });
    state.fileEmails = payload.emails;
    el("file-title").textContent = file.name;
    el("file-meta").textContent = `${payload.count.toLocaleString()} 个邮箱`;
  } catch (error) {
    el("file-title").textContent = "选择文件";
    el("file-meta").textContent = "TXT · CSV · JSON · XLSX · XLSM · XLS";
    errorBox.textContent = error.message;
  }
  updateCount();
}

el("file-input").addEventListener("change", (event) => importFile(event.target.files[0]));
const dropzone = el("file-dropzone");
["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
}));
dropzone.addEventListener("drop", (event) => importFile(event.dataTransfer.files[0]));

startButton.addEventListener("click", async () => {
  const emails = currentEmails();
  errorBox.textContent = "";
  if (!emails.length) {
    errorBox.textContent = state.view === "single" ? "请输入一个邮箱地址" : "请至少输入一个邮箱地址";
    return;
  }
  if (state.view === "single" && emails.length !== 1) {
    errorBox.textContent = "单个验证一次只能提交一个邮箱地址";
    return;
  }
  startButton.disabled = true;
  startButton.textContent = "正在提交…";
  try {
    state.guestToken = null;
    const isFreeSingle = state.view === "single";
    const workerCount = isFreeSingle ? 1 : Number(document.querySelector('input[name="speed"]:checked').value);
    const job = await api(isFreeSingle ? "/api/verify/single" : "/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(isFreeSingle
        ? { email: emails[0] }
        : { emails, worker_count: workerCount }),
    });
    state.jobId = job.id;
    state.guestToken = job.access_token || null;
    sessionStorage.setItem("verigo_job_id", state.jobId);
    if (state.guestToken) sessionStorage.setItem("verigo_job_token", state.guestToken);
    else sessionStorage.removeItem("verigo_job_token");
    state.results = [];
    state.resultsAvailable = 0;
    state.page = 0;
    showJob(job);
    renderResults();
    if (state.user) await loadAccount();
    schedulePoll(400);
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    startButton.disabled = false;
    updateCount();
  }
});

function showJob(job) {
  state.jobId = job.id;
  state.downloadName = job.download_name || null;
  el("job-title").textContent = formatJobName(job.finished_at || job.started_at || job.created_at);
  const status = el("job-status");
  status.textContent = statusLabels[job.status] || job.status;
  status.className = `status status-${job.status}`;
  const mode = el("job-mode");
  const [modeLabel, modeClass] = job.qq_slow
    ? ["QQ 专属低并发", "mode-qq"]
    : (modeLabels[job.worker_count] || ["自定义模式", "mode-standard"]);
  mode.textContent = modeLabel;
  mode.className = `mode-badge ${modeClass}`;
  const isActive = job.status === "queued" || job.status === "running";
  el("stop-job-button").classList.toggle("hidden", !isActive);
  el("stop-job-button").disabled = !isActive;
  el("resume-job-button").classList.toggle("hidden", job.status !== "stopped");
  el("resume-job-button").disabled = job.status !== "stopped";
  el("progress-percent").textContent = `${job.progress}%`;
  el("progress-bar").style.width = `${job.progress}%`;
  const progressCopy = job.error
    || (job.status === "queued" && job.queue_position ? `排队中，前方还有 ${job.queue_position - 1} 个任务` : `${job.completed} / ${job.total} 已处理`);
  el("progress-copy").textContent = job.qq_slow
    ? `${progressCopy}；QQ 邮箱采用低并发和自动退避策略，请耐心等待。`
    : progressCopy;
  if (job.summary) renderSummary(job.summary);
  el("download-button").disabled = !job.download_url;
}

function formatJobName(timestamp) {
  if (!timestamp) return "邮箱验证";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "邮箱验证";
  return `邮箱验证 ${new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date)}`;
}

function renderSummary(summary = {}) {
  document.querySelectorAll("#summary [data-key]").forEach((node) => {
    node.textContent = Number(summary[node.dataset.key] || 0).toLocaleString();
  });
}

function schedulePoll(delay = 1300) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(pollJob, delay);
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const job = await api(`/api/jobs/${state.jobId}`);
    showJob(job);
    await loadResults();
    if (job.status === "completed" || job.status === "stopped") {
      if (state.user) await loadRecentJobs();
    } else if (job.status !== "failed") {
      schedulePoll();
    }
  } catch (error) {
    errorBox.textContent = error.message;
  }
}

async function loadResults() {
  const offset = state.page * pageSize;
  const search = encodeURIComponent(el("result-search").value.trim());
  const deliverability = encodeURIComponent(el("result-filter").value);
  const baseUrl = `/api/jobs/${state.jobId}/results?limit=${pageSize}&search=${search}&deliverability=${deliverability}`;
  let payload = await api(`${baseUrl}&offset=${offset}`);
  if (payload.available && offset >= payload.available && state.page > 0) {
    state.page = Math.ceil(payload.available / pageSize) - 1;
    payload = await api(`${baseUrl}&offset=${state.page * pageSize}`);
  }
  state.results = payload.items;
  state.resultsAvailable = payload.available;
  renderResults();
}

function resultMeta(item) {
  if (item.skipped) return ["已停止", "result-skipped", "skipped"];
  if (item.deliverable === true) return ["可投递", "result-good", "deliverable"];
  if (item.deliverable === false) return ["不可投递", "result-bad", "undeliverable"];
  return ["待确认", "result-unknown", "unknown"];
}

function renderResults() {
  const body = el("results-body");
  const rows = state.results;
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    row.className = "empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = state.results.length ? "没有符合条件的结果" : "正在等待首条验证结果";
    row.append(cell);
    body.append(row);
    renderPagination();
    return;
  }
  rows.forEach((item) => {
    const [label, className] = resultMeta(item);
    const row = document.createElement("tr");
    const values = [item.email, null, item.domain_type || "-", item.verification_method || item.strategy || "-", item.smtp_result || item.message || "-"];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 1) {
        const pill = document.createElement("span");
        pill.className = `result-pill ${className}`;
        pill.textContent = label;
        cell.append(pill);
      } else {
        cell.textContent = String(value ?? "-");
        if (index === 4) cell.className = "detail-cell";
      }
      row.append(cell);
    });
    body.append(row);
  });
  renderPagination();
}

function renderPagination() {
  const available = state.resultsAvailable;
  const start = available ? state.page * pageSize + 1 : 0;
  const end = Math.min((state.page + 1) * pageSize, available);
  el("results-page-info").textContent = available ? `已显示 ${start}-${end}，共 ${available} 条已出结果` : "等待验证结果";
  el("previous-page").disabled = state.page === 0;
  el("next-page").disabled = (state.page + 1) * pageSize >= available;
}

let searchTimer = null;
el("result-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  state.page = 0;
  searchTimer = setTimeout(() => loadResults(), 250);
});
el("result-filter").addEventListener("change", async () => {
  state.page = 0;
  await loadResults();
});
el("previous-page").addEventListener("click", async () => {
  if (state.page === 0) return;
  state.page -= 1;
  await loadResults();
});
el("next-page").addEventListener("click", async () => {
  if ((state.page + 1) * pageSize >= state.resultsAvailable) return;
  state.page += 1;
  await loadResults();
});
el("download-button").addEventListener("click", async () => {
  if (!state.jobId) return;
  try {
    const response = await fetch(`/api/jobs/${state.jobId}/download`, { headers: jobHeaders() });
    if (!response.ok) throw new Error("下载失败");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = state.downloadName || "Verigo-邮箱验证结果.csv";
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    errorBox.textContent = error.message;
  }
});
el("stop-job-button").addEventListener("click", async () => {
  if (!state.jobId) return;
  const button = el("stop-job-button");
  button.disabled = true;
  try {
    const job = await api(`/api/jobs/${state.jobId}/stop`, { method: "POST" });
    clearTimeout(state.pollTimer);
    showJob(job);
    await loadResults();
    if (state.user) await loadRecentJobs();
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
el("resume-job-button").addEventListener("click", async () => {
  if (!state.jobId) return;
  const button = el("resume-job-button");
  button.disabled = true;
  try {
    const job = await api(`/api/jobs/${state.jobId}/resume`, { method: "POST" });
    state.page = 0;
    state.results = [];
    showJob(job);
    await loadResults();
    schedulePoll(300);
    if (state.user) await loadRecentJobs();
  } catch (error) {
    errorBox.textContent = error.message;
    button.disabled = false;
  }
});

function resultType(item) {
  return resultMeta(item)[2];
}

function renderDiscoveryResults() {
  const body = el("discovery-results-body");
  body.replaceChildren();
  if (!state.discovery.results.length) {
    if (state.discovery.candidates.length && !state.discovery.jobId) {
      state.discovery.candidates.forEach((email) => {
        const row = document.createElement("tr");
        [email, "未验证", "-", "-"].forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        });
        body.append(row);
      });
    } else {
      const row = document.createElement("tr");
      row.className = "empty-row";
      row.innerHTML = '<td colspan="4">正在生成验证结果</td>';
      body.append(row);
    }
    return;
  }
  state.discovery.results.forEach((item) => {
    const [label, className] = resultMeta(item);
    const row = document.createElement("tr");
    [item.email, label, item.verification_method || item.strategy || "-", item.smtp_result || item.message || "-"].forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 1) {
        const pill = document.createElement("span");
        pill.className = `result-pill ${className}`;
        pill.textContent = value;
        cell.append(pill);
      } else cell.textContent = String(value);
      row.append(cell);
    });
    body.append(row);
  });
}

function showDiscoveryJob(job) {
  el("discovery-title").textContent = `查找 ${job.total} 个候选邮箱`;
  const status = el("discovery-status");
  status.textContent = statusLabels[job.status] || job.status;
  status.className = `status status-${job.status}`;
  const isActive = job.status === "queued" || job.status === "running";
  el("discovery-stop-button").classList.toggle("hidden", !isActive);
  el("discovery-stop-button").disabled = !isActive;
  el("discovery-progress-percent").textContent = `${job.progress}%`;
  el("discovery-progress-bar").style.width = `${job.progress}%`;
  const progressCopy = job.status === "queued" && job.queue_position
    ? `排队中，前方还有 ${job.queue_position - 1} 个任务`
    : `${job.completed} / ${job.total} 已处理`;
  el("discovery-progress-copy").textContent = job.qq_slow
    ? `${progressCopy}；QQ 邮箱采用低并发和自动退避策略，请耐心等待。`
    : progressCopy;
}

function updateDiscoveryVerdict(job) {
  const verdict = el("discovery-verdict");
  if (job.status === "stopped") {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = "验证已停止，已保留当前结果。";
    return;
  }
  if (job.status !== "completed") {
    verdict.className = "discovery-verdict";
    verdict.textContent = "正在从候选地址中确认结果";
    return;
  }
  const good = state.discovery.results.filter((item) => resultType(item) === "deliverable");
  const unknown = state.discovery.results.filter((item) => resultType(item) === "unknown");
  if (good.length === 1) {
    verdict.className = "discovery-verdict good";
    verdict.textContent = `已找到唯一可确认邮箱：${good[0].email}`;
  } else if (good.length > 1) {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = `找到 ${good.length} 个可确认地址，请结合职位或公开信息进一步确认。`;
  } else if (unknown.length) {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = "没有可确认地址，部分候选暂时无法确认。请稍后重试或检查域名。";
  } else {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = "未找到可确认地址。请检查姓名和域名，或对方可能已离职。";
  }
}

async function loadDiscoveryResults() {
  const payload = await api(`/api/jobs/${state.discovery.jobId}/results?offset=0&limit=100`);
  state.discovery.results = payload.items;
  renderDiscoveryResults();
}

async function pollDiscovery() {
  if (!state.discovery.jobId) return;
  try {
    const job = await api(`/api/jobs/${state.discovery.jobId}`);
    showDiscoveryJob(job);
    await loadDiscoveryResults();
    updateDiscoveryVerdict(job);
    if (job.status !== "completed" && job.status !== "failed" && job.status !== "stopped") setTimeout(pollDiscovery, 1200);
  } catch (error) {
    el("discovery-error").textContent = error.message;
  }
}

el("discovery-start").addEventListener("click", async () => {
  const error = el("discovery-error");
  error.textContent = "";
  if (isYahooEmail(`probe@${el("discovery-domain").value}`)) {
    error.textContent = yahooUnsupportedMessage;
    return;
  }
  const button = el("discovery-start");
  button.disabled = true;
  try {
    const candidates = await api("/api/discovery/candidates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        first_name: el("discovery-first-name").value,
        last_name: el("discovery-last-name").value,
        domain: el("discovery-domain").value,
      }),
    });
    state.discovery.jobId = null;
    state.discovery.candidates = candidates.candidates;
    state.discovery.results = [];
    const list = el("discovery-candidates");
    list.replaceChildren(...state.discovery.candidates.map((email) => {
      const tag = document.createElement("span");
      tag.textContent = email;
      return tag;
    }));
    list.classList.remove("hidden");
    const verifyButton = el("discovery-verify");
    verifyButton.disabled = false;
    verifyButton.textContent = `免费验证候选邮箱 · ${state.discovery.candidates.length} 个地址`;
    el("discovery-title").textContent = `${state.discovery.candidates.length} 个候选邮箱`;
    el("discovery-status").textContent = "已找到";
    el("discovery-status").className = "status status-completed";
    el("discovery-progress-percent").textContent = "0%";
    el("discovery-progress-bar").style.width = "0%";
    el("discovery-progress-copy").textContent = "等待验证";
    const hasQqCandidate = state.discovery.candidates.some(isQqEmail);
    el("discovery-verdict").className = hasQqCandidate ? "discovery-verdict warn" : "discovery-verdict";
    el("discovery-verdict").textContent = hasQqCandidate
      ? `已生成 ${state.discovery.candidates.length} 个候选地址。QQ 邮箱验证采用专属低并发策略，验证速度较慢，请耐心等待。`
      : `已生成 ${state.discovery.candidates.length} 个候选地址`;
    renderDiscoveryResults();
  } catch (requestError) {
    error.textContent = requestError.message;
  } finally {
    button.disabled = false;
  }
});

el("discovery-verify").addEventListener("click", async () => {
  const error = el("discovery-error");
  const button = el("discovery-verify");
  error.textContent = "";
  if (!state.discovery.candidates.length) return;
  if (state.discovery.candidates.some(isYahooEmail)) {
    error.textContent = yahooUnsupportedMessage;
    return;
  }
  button.disabled = true;
  let submitted = false;
  try {
    const job = await api("/api/discovery/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        first_name: el("discovery-first-name").value,
        last_name: el("discovery-last-name").value,
        domain: el("discovery-domain").value,
      }),
    });
    state.discovery.jobId = job.id;
    submitted = true;
    state.discovery.results = [];
    renderDiscoveryResults();
    showDiscoveryJob(job);
    updateDiscoveryVerdict(job);
    await loadAccount();
    pollDiscovery();
  } catch (requestError) {
    error.textContent = requestError.message;
  } finally {
    button.disabled = submitted || !state.discovery.candidates.length;
  }
});
el("discovery-stop-button").addEventListener("click", async () => {
  if (!state.discovery.jobId) return;
  const button = el("discovery-stop-button");
  button.disabled = true;
  try {
    const job = await api(`/api/jobs/${state.discovery.jobId}/stop`, { method: "POST" });
    showDiscoveryJob(job);
    await loadDiscoveryResults();
    updateDiscoveryVerdict(job);
    await loadRecentJobs();
  } catch (error) {
    el("discovery-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function loadRecentJobs() {
  if (!state.user) return;
  try {
    const jobs = await api("/api/jobs?limit=8");
    const container = el("recent-jobs");
    container.replaceChildren();
    if (!jobs.length) {
      const empty = document.createElement("small");
      empty.textContent = "暂无任务";
      container.append(empty);
      return;
    }
    jobs.forEach((job) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "recent-job";
      const name = document.createElement("span");
      name.textContent = formatJobName(job.finished_at || job.started_at || job.created_at);
      const meta = document.createElement("small");
      meta.textContent = `${statusLabels[job.status] || job.status} · ${job.total}`;
      button.append(name, meta);
      button.addEventListener("click", async () => {
        clearTimeout(state.pollTimer);
        state.guestToken = null;
        sessionStorage.removeItem("verigo_job_token");
        state.results = [];
        state.resultsAvailable = 0;
        state.page = 0;
        showJob(job);
        renderResults();
        await loadResults();
        if (job.status !== "completed" && job.status !== "failed") schedulePoll(300);
      });
      container.append(button);
    });
  } catch (error) {
    errorBox.textContent = error.message;
  }
}

function updateAccount() {
  el("account-button").textContent = state.user ? state.user.email : "登录";
  el("account-name").textContent = state.user?.email || "";
  const trialCredits = Number(state.user?.trial_credits || 0);
  el("account-credits").textContent = state.user
    ? state.user.is_admin
      ? "无限额度"
      : `${state.user.credits || 0} 验证次数${trialCredits ? ` · ${trialCredits} 体验次数` : ""}`
    : "";
  el("account-credits").title = state.user?.trial_credit_expires_at
    ? `体验额度有效至 ${new Date(state.user.trial_credit_expires_at).toLocaleString("zh-CN")}`
    : "";
  el("bind-email-button").classList.toggle("hidden", !state.user?.needs_email_binding);
  el("dashboard-nav").classList.toggle("hidden", !state.user?.is_admin);
  el("admin-credits-nav").classList.toggle("hidden", !state.user?.is_admin);
  el("notification-button").classList.toggle("hidden", !state.user);
  el("claim-trial-button").classList.toggle(
    "hidden", !state.user || state.user.needs_email_binding || state.user.email_verified,
  );
  el("recent-block").classList.toggle("hidden", !state.user);
  el("account-menu").classList.add("hidden");
  el("notification-menu").classList.add("hidden");
  clearInterval(state.notificationTimer);
  state.notificationTimer = null;
  if (state.user) {
    loadRecentJobs();
    loadNotifications();
    state.notificationTimer = window.setInterval(loadNotifications, 60000);
  }
}

async function loadAccount() {
  try { state.user = await api("/api/auth/me"); } catch (_) { state.user = null; }
  updateAccount();
}

el("dashboard-refresh").addEventListener("click", loadDashboardMetrics);
async function loadWallet() { const data = await api("/api/wallet"); const set=(id,v)=>el(id).textContent=Number(v||0).toLocaleString("zh-CN"); set("wallet-available",data.available_verifications); set("wallet-paid",data.paid_verifications); set("wallet-trial",data.trial_verifications); set("wallet-used",data.verifications_used); el("wallet-price").textContent=`100 次 ¥${(data.price_fen_per_100/100).toFixed(2)}`; el("wallet-trial-note").textContent=data.trial_expires_at?`有效至 ${new Date(data.trial_expires_at).toLocaleDateString("zh-CN")}`:"无体验次数"; el("wallet-updated").textContent=`更新于 ${new Date().toLocaleString("zh-CN")}`; const days=data.usage_daily||[]; const max=Math.max(1,...days.map(x=>x.verifications)); el("wallet-usage-chart").innerHTML=days.map(x=>`<div class="wallet-bar" style="height:${Math.max(4,x.verifications/max*180)}px"><span>${x.verifications}</span></div>`).join(""); el("wallet-transactions").innerHTML=(data.transactions||[]).map(x=>`<div class="wallet-transaction"><div><strong>${x.title}</strong><small>${x.credits>0?"+":""}${x.credits} 次 ${x.note||""}</small></div><div><strong>${x.amount_fen==null?"—":`${x.credits<0?"-":"+"}¥${(x.amount_fen/100).toFixed(2)}`}</strong><small>${new Date(x.created_at).toLocaleString("zh-CN")}</small></div></div>`).join("")||"暂无资金流水"; }
el("wallet-refresh").addEventListener("click", loadWallet); el("wallet-button").addEventListener("click",()=>switchView("wallet"));
el("admin-account-lookup").addEventListener("click", async()=>{ const box=el("admin-account-summary"); try { const d=await api(`/api/admin/accounts?email=${encodeURIComponent(el("admin-credit-email").value)}`); box.classList.remove("hidden"); const audit=(d.adjustments||[]).map(x=>`<li>${x.delta>0?"+":""}${x.delta} 次 · ${x.amount_fen==null?"未登记金额":"¥"+(x.amount_fen/100).toFixed(2)} · ${x.note||"无备注"}</li>`).join("")||"<li>暂无管理员调整记录</li>"; box.innerHTML=`<strong>${d.email}</strong><dl><div><dt>可用次数</dt><dd>${d.available_verifications}</dd></div><div><dt>付费次数</dt><dd>${d.paid_verifications}</dd></div><div><dt>累计已验证</dt><dd>${d.verifications_used}</dd></div></dl><p>最近调整记录</p><ul>${audit}</ul>`; } catch(e){box.classList.remove("hidden");box.textContent=e.message;} });
function renderNotifications() {
  const list = el("notification-list");
  list.replaceChildren();
  if (!state.notifications.length) {
    const empty = document.createElement("p");
    empty.className = "notification-empty";
    empty.textContent = "暂无通知";
    list.append(empty);
    return;
  }
  state.notifications.forEach((notification) => {
    const item = document.createElement("article");
    item.className = "notification-item";
    const title = document.createElement("strong");
    title.textContent = notification.title;
    const body = document.createElement("p");
    body.textContent = notification.body;
    const time = document.createElement("time");
    time.textContent = new Date(notification.created_at).toLocaleString("zh-CN");
    item.append(title, body, time);
    list.append(item);
  });
}

async function loadNotifications() {
  if (!state.user) return;
  try {
    const payload = await api("/api/notifications");
    state.notifications = payload.items;
    el("notification-count").textContent = payload.unread_count > 99 ? "99+" : String(payload.unread_count);
    el("notification-count").classList.toggle("hidden", !payload.unread_count);
    renderNotifications();
  } catch (_) {
    state.notifications = [];
  }
}

el("notification-button").addEventListener("click", async () => {
  const menu = el("notification-menu");
  const opening = menu.classList.contains("hidden");
  menu.classList.toggle("hidden", !opening);
  el("account-menu").classList.add("hidden");
  if (!opening) return;
  await loadNotifications();
  if (!el("notification-count").classList.contains("hidden")) {
    await api("/api/notifications/read", { method: "POST" });
    await loadNotifications();
  }
});
document.addEventListener("click", (event) => {
  if (!el("notification-menu").contains(event.target) && !el("notification-button").contains(event.target)) el("notification-menu").classList.add("hidden");
  if (!el("account-menu").contains(event.target) && !el("account-button").contains(event.target)) el("account-menu").classList.add("hidden");
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { el("notification-menu").classList.add("hidden"); el("account-menu").classList.add("hidden"); }
});
el("admin-credit-grant-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = el("admin-credit-submit");
  const result = el("admin-credit-result");
  submit.disabled = true;
  result.className = "admin-credit-result";
  result.textContent = "";
  try {
    const action = el("admin-credit-action").value;
    const adjustment = await api(`/api/admin/credits/${action === "deduct" ? "deduct" : "grant"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: el("admin-credit-email").value,
        credits: Number(el("admin-credit-amount").value),
        note: el("admin-credit-note").value,
        amount_fen: el("admin-credit-payment").value === "" ? null : Math.round(Number(el("admin-credit-payment").value) * 100),
      }),
    });
    result.classList.add("success");
    const amount = Math.abs(adjustment.delta).toLocaleString("zh-CN");
    result.textContent = action === "deduct"
      ? `已从 ${adjustment.email} 扣减 ${amount} 额度，当前余额 ${adjustment.credits.toLocaleString("zh-CN")}。`
      : `已向 ${adjustment.email} 授予 ${amount} 额度，当前余额 ${adjustment.credits.toLocaleString("zh-CN")}。`;
    el("admin-credit-amount").value = "";
    el("admin-credit-note").value = "";
    el("admin-credit-payment").value = "";
  } catch (error) {
    result.classList.add("error");
    result.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

el("account-button").addEventListener("click", () => {
  if (state.user) el("account-menu").classList.toggle("hidden");
  else el("auth-dialog").showModal();
});
el("logout-button").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.user = null;
  updateAccount();
});
el("delete-account-button").addEventListener("click", () => {
  el("account-menu").classList.add("hidden");
  el("delete-account-confirm").checked = false;
  el("delete-account-error").textContent = "";
  el("delete-account-dialog").showModal();
});
el("change-password-button").addEventListener("click", () => {
  el("account-menu").classList.add("hidden");
  el("change-password-form").reset();
  el("change-password-error").textContent = "";
  el("change-password-dialog").showModal();
});
el("close-change-password").addEventListener("click", () => el("change-password-dialog").close());
el("change-password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  el("change-password-error").textContent = "";
  try {
    await api("/api/auth/password/change", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: el("current-password").value,
        new_password: el("new-password").value,
      }),
    });
    el("change-password-dialog").close();
  } catch (error) {
    el("change-password-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
el("close-delete-account").addEventListener("click", () => el("delete-account-dialog").close());
el("delete-account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type='submit']");
  submit.disabled = true;
  el("delete-account-error").textContent = "";
  try {
    await api("/api/auth/account", { method: "DELETE" });
    state.user = null;
    el("delete-account-dialog").close();
    updateAccount();
  } catch (error) {
    el("delete-account-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
function claimTrialCredits() {
  el("email-verification-request-form").classList.remove("hidden");
  el("email-verification-confirm-form").classList.add("hidden");
  el("email-verification-error").textContent = "";
  el("email-verification-confirm-error").textContent = "";
  el("email-verification-code").value = "";
  el("email-verification-dialog").showModal();
}

el("claim-trial-button").addEventListener("click", claimTrialCredits);
el("close-email-verification").addEventListener("click", () => el("email-verification-dialog").close());
document.querySelectorAll("[data-close-email-verification]").forEach((button) => {
  button.addEventListener("click", () => el("email-verification-dialog").close());
});
el("email-verification-request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type='submit']");
  submit.disabled = true;
  el("email-verification-error").textContent = "";
  try {
    await api("/api/auth/email-verification/request", { method: "POST" });
    el("email-verification-request-form").classList.add("hidden");
    el("email-verification-confirm-form").classList.remove("hidden");
    el("email-verification-code").focus();
  } catch (error) {
    el("email-verification-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
el("email-verification-confirm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type='submit']");
  submit.disabled = true;
  el("email-verification-confirm-error").textContent = "";
  try {
    state.user = await api("/api/auth/email-verification/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: el("email-verification-code").value }),
    });
    el("email-verification-dialog").close();
    updateAccount();
  } catch (error) {
    el("email-verification-confirm-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

function openBindEmailDialog() {
  el("account-menu").classList.add("hidden");
  el("bind-email-request-form").classList.remove("hidden");
  el("bind-email-confirm-form").classList.add("hidden");
  el("bind-email-error").textContent = "";
  el("bind-email-confirm-error").textContent = "";
  el("bind-email-dialog").showModal();
}

el("bind-email-button").addEventListener("click", openBindEmailDialog);
el("close-bind-email").addEventListener("click", () => el("bind-email-dialog").close());
document.querySelectorAll("[data-close-bind-email]").forEach((button) => {
  button.addEventListener("click", () => el("bind-email-dialog").close());
});
el("bind-email-request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  el("bind-email-error").textContent = "";
  try {
    await api("/api/auth/email-binding/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: el("bind-email").value }),
    });
    el("bind-email-request-form").classList.add("hidden");
    el("bind-email-confirm-form").classList.remove("hidden");
  } catch (error) { el("bind-email-error").textContent = error.message; }
});
el("bind-email-confirm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  el("bind-email-confirm-error").textContent = "";
  try {
    state.user = await api("/api/auth/email-binding/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: el("bind-email-code").value }),
    });
    el("bind-email-dialog").close();
    updateAccount();
  } catch (error) { el("bind-email-confirm-error").textContent = error.message; }
});

function setAuthMode(mode) {
  state.authMode = mode;
  el("auth-title").textContent = mode === "login" ? "登录" : "创建账户";
  el("auth-submit").textContent = mode === "login" ? "登录" : "注册";
  el("auth-account-label").textContent = mode === "login" ? "邮箱或旧用户名" : "邮箱";
  el("auth-email").type = mode === "login" ? "text" : "email";
  el("auth-email").autocomplete = mode === "login" ? "username" : "email";
  el("auth-password").autocomplete = mode === "login" ? "current-password" : "new-password";
  el("turnstile-container").classList.toggle("hidden", mode !== "register" || !state.turnstileSiteKey);
  if (mode === "register") renderTurnstile();
  document.querySelectorAll("[data-auth-mode]").forEach((button) => button.classList.toggle("active", button.dataset.authMode === mode));
  el("auth-error").textContent = "";
}

function renderTurnstile() {
  if (!state.turnstileSiteKey || !window.turnstile || state.turnstileWidgetId !== null) return;
  state.turnstileWidgetId = window.turnstile.render("#turnstile-widget", {
    sitekey: state.turnstileSiteKey,
    theme: "light",
  });
}

async function loadPublicConfig() {
  try {
    const config = await api("/api/auth/public-config");
    state.turnstileSiteKey = config.turnstile_site_key || "";
    if (!state.turnstileSiteKey) return;
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    script.defer = true;
    script.addEventListener("load", () => {
      el("turnstile-container").classList.toggle("hidden", state.authMode !== "register");
      renderTurnstile();
    });
    document.head.append(script);
  } catch (_) {
    state.turnstileSiteKey = "";
  }
}

document.querySelectorAll("[data-auth-mode]").forEach((button) => button.addEventListener("click", () => setAuthMode(button.dataset.authMode)));
el("close-auth").addEventListener("click", () => el("auth-dialog").close());
el("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = el("auth-submit");
  submit.disabled = true;
  el("auth-error").textContent = "";
  try {
    state.user = await api(`/api/auth/${state.authMode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        [state.authMode === "login" ? "account" : "email"]: el("auth-email").value,
        password: el("auth-password").value,
        ...(state.authMode === "register" && state.turnstileSiteKey ? {
          turnstile_token: window.turnstile?.getResponse(state.turnstileWidgetId) || "",
        } : {}),
      }),
    });
    el("auth-dialog").close();
    el("auth-form").reset();
    updateAccount();
    if (window.location.pathname === "/dashboard" && state.user.is_admin) switchView("dashboard");
    if (window.location.pathname === "/admin/credits" && state.user.is_admin) switchView("admin-credits");
  } catch (error) {
    el("auth-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

el("open-reset").addEventListener("click", () => {
  el("auth-dialog").close();
  el("reset-request-form").classList.remove("hidden");
  el("reset-confirm-form").classList.add("hidden");
  el("reset-error").textContent = "";
  el("reset-dialog").showModal();
});
el("close-reset").addEventListener("click", () => el("reset-dialog").close());
document.querySelectorAll("[data-close-reset]").forEach((button) => button.addEventListener("click", () => el("reset-dialog").close()));
el("reset-request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  el("reset-error").textContent = "";
  try {
    await api("/api/auth/password-reset/request", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: el("reset-email").value }),
    });
    el("reset-request-form").classList.add("hidden");
    el("reset-confirm-form").classList.remove("hidden");
  } catch (error) { el("reset-error").textContent = error.message; }
});
el("reset-confirm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  el("reset-confirm-error").textContent = "";
  try {
    await api("/api/auth/password-reset/confirm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: el("reset-email").value, code: el("reset-code").value, password: el("reset-password").value }),
    });
    el("reset-dialog").close();
    el("auth-dialog").showModal();
    setAuthMode("login");
  } catch (error) { el("reset-confirm-error").textContent = error.message; }
});

el("refresh-jobs").addEventListener("click", loadRecentJobs);

(async function init() {
  setAuthMode(state.authMode);
  updateCount();
  await loadAccount();
  await loadPublicConfig();
  if (["/dashboard", "/admin/credits", "/wallet"].includes(window.location.pathname)) {
    if (state.user?.is_admin) {
      switchView(window.location.pathname === "/admin/credits" ? "admin-credits" : window.location.pathname === "/wallet" ? "wallet" : "dashboard");
    } else if (state.user) {
      window.location.replace("/");
      return;
    } else {
      el("auth-dialog").showModal();
      setAuthMode("login");
      el("auth-error").textContent = "请登录有运营监控权限的账户";
    }
  }
  if (state.jobId) {
    try {
      const job = await api(`/api/jobs/${state.jobId}`);
      showJob(job);
      await loadResults();
      if (job.status !== "completed" && job.status !== "failed") schedulePoll(400);
    } catch (_) {
      sessionStorage.removeItem("verigo_job_id");
      sessionStorage.removeItem("verigo_job_token");
      state.jobId = null;
      state.guestToken = null;
    }
  }
})();
