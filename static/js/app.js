const state = {
  view: window.location.pathname === "/lists" ? "lists"
    : window.location.pathname.startsWith("/lists/") ? "lists"
    : window.location.pathname === "/workspace" ? "workspace"
    : window.location.pathname === "/dashboard"
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
  recentJobs: { offset: 0, limit: 8, total: 0 },
  adminAccountOffset: 0,
  retryCountdownTimer: null,
  onboardingTimer: null,
  history: { offset: 0, limit: 10, total: 0 },
  workspace: { loaded: false },
  pendingView: null,
  pendingSaveResult: null,
  activeResultItem: null,
  listSelection: new Set(),
  discoverySelection: new Set(),
};

const pageSize = 50;

function pageWindow(current, pageCount) {
  const pages = new Set([0, pageCount - 1]);
  for (let page = Math.max(0, current - 2); page <= Math.min(pageCount - 1, current + 2); page += 1) pages.add(page);
  return [...pages].sort((left, right) => left - right);
}

function renderPageNumbers(container, current, pageCount, onSelect) {
  container.replaceChildren();
  let previous = -1;
  pageWindow(current, pageCount).forEach((page) => {
    if (page > previous + 1) {
      const gap = document.createElement("span"); gap.className = "page-gap"; gap.textContent = "鈥?; container.append(gap);
    }
    const button = document.createElement("button"); button.type = "button";
    button.className = `page-number${page === current ? " active" : ""}`;
    button.textContent = String(page + 1); button.disabled = page === current;
    button.addEventListener("click", () => onSelect(page)); container.append(button);
    previous = page;
  });
}

const el = (id) => document.getElementById(id);
const batchInput = el("email-input");
const singleInput = el("single-email-input");
const count = el("email-count");
const startButton = el("start-button");
const errorBox = el("form-error");
VerigoI18n.init();
const statusLabels = { queued: "鎺掗槦涓?, running: "楠岃瘉涓?, completed: "宸插畬鎴?, failed: "澶辫触", stopped: "宸插仠姝? };
const modeLabels = {
  1: ["楠岃瘉浠诲姟", "mode-standard"],
  2: ["楠岃瘉浠诲姟", "mode-standard"],
  4: ["楠岃瘉浠诲姟", "mode-standard"],
  8: ["楠岃瘉浠诲姟", "mode-standard"],
};

function splitEmails(text) {
  return text.split(/[\s,;锛岋紱]+/).map((value) => value.trim()).filter((value) => value.includes("@"));
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

const yahooUnsupportedMessage = "鏆備笉鏀寔 Yahoo 閭楠岃瘉锛堝惈鎵€鏈夊浗瀹舵垨鍦板尯鍚庣紑锛屼互鍙?ymail.com銆乺ocketmail.com锛夈€俌ahoo 鐨勫弽楠岃瘉绛栫暐闈炲父涓ユ牸锛屽綋鍓嶅叏缃戝父瑙勯獙璇佸潎闅句互绋冲畾閫氳繃锛屾殏鏃舵病鏈夊彲闈犺В鍐虫柟妗堛€?;

function updateProviderNotice(emails) {
  const notice = el("qq-rate-notice");
  const hasQq = emails.some(isQqEmail);
  notice.classList.toggle("hidden", !hasQq);
  notice.textContent = hasQq
    ? VerigoI18n.text("妫€娴嬪埌 QQ 閭锛氬皢閲囩敤涓撳睘浣庡苟鍙戜笌鑷姩閫€閬跨瓥鐣ワ紝楠岃瘉閫熷害浼氳緝鎱紝璇疯€愬績绛夊緟銆?)
    : "";
}

function updateCount() {
  const total = currentEmails().length;
  updateProviderNotice(currentEmails());
  count.textContent = total.toLocaleString();
  if (state.view === "single") {
    startButton.textContent = VerigoI18n.text("鍏嶈垂楠岃瘉");
  } else if (total > 0) {
    startButton.textContent = VerigoI18n.text(`寮€濮嬮獙璇?路 ${total.toLocaleString()} 棰濆害`);
  } else {
    startButton.textContent = VerigoI18n.text("寮€濮嬮獙璇?);
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
    const message = Array.isArray(detail) ? detail.map((item) => item.msg).join("锛?) : detail;
    throw new Error(VerigoI18n.errorMessage(message || `璇锋眰澶辫触 (${response.status})`));
  }
  return body;
}

function switchView(view) {
  const adminView = view === "dashboard" || view === "admin-credits";
  if (adminView && !state.user?.is_admin) {
    if (!state.user) {
      el("auth-dialog").showModal();
      setAuthMode("login");
      el("auth-error").textContent = "璇峰厛鐧诲綍绠＄悊鍛樿处鎴?;
    }
    return;
  }
  const discovery = view === "discovery";
  const dashboard = view === "dashboard";
  const adminCredits = view === "admin-credits";
  const wallet = view === "wallet";
  const history = view === "history";
  const workspace = view === "workspace";
  const lists = view === "lists";
  if ((wallet || history || lists) && !state.user) { state.pendingView = view; el("auth-dialog").showModal(); setAuthMode("login"); el("auth-error").textContent = VerigoI18n.text("璇峰厛鐧诲綍鍚庢煡鐪嬭处鎴锋暟鎹?); return; }
  if (workspace && !state.user) { state.pendingView = "workspace"; el("auth-dialog").showModal(); setAuthMode("login"); el("auth-error").textContent = "Please sign in to open your workspace"; return; }
  if (discovery && !state.user) {
    el("auth-dialog").showModal();
    setAuthMode("login");
    el("auth-error").textContent = "璇峰厛鐧诲綍鍚庝娇鐢ㄤ紒涓氶偖绠辨煡鎵?;
    return;
  }
  state.view = view;
  document.querySelectorAll(".public-marketing").forEach((section) => section.classList.toggle("workspace-mode-hidden", workspace || lists));
  el("verify-workspace").classList.toggle("hidden", discovery || dashboard || adminCredits || wallet || history || workspace || lists);
  el("workspace-home").classList.toggle("hidden", !workspace);
  el("discovery-workspace").classList.toggle("hidden", !discovery);
  el("dashboard-workspace").classList.toggle("hidden", !dashboard);
  el("admin-credits-workspace").classList.toggle("hidden", !adminCredits);
  el("wallet-workspace").classList.toggle("hidden", !wallet);
  el("history-workspace").classList.toggle("hidden", !history);
  el("lists-workspace").classList.toggle("hidden", !lists);
  el("single-panel").classList.toggle("hidden", view !== "single");
  el("batch-panel").classList.toggle("hidden", view !== "batch");
  if (!discovery && !dashboard && !adminCredits && !wallet && !history && !workspace && !lists) {
    el("verify-eyebrow").textContent = VerigoI18n.text(view === "single" ? "鍏嶈垂鍗曚釜楠岃瘉" : "鏀惰垂鎵归噺楠岃瘉");
    el("verify-heading").textContent = VerigoI18n.text(view === "single" ? "楠岃瘉鍗曚釜鏀朵欢鍦板潃" : "鎵归噺楠岃瘉鏀朵欢鍦板潃");
  }
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  if (dashboard) {
    document.title = `${VerigoI18n.text("杩愯惀鐩戞帶")} | Verigo`;
    if (window.location.pathname !== "/dashboard") window.history.pushState({}, "", "/dashboard");
    loadDashboardMetrics();
    clearInterval(state.metricsTimer);
    state.metricsTimer = window.setInterval(loadDashboardMetrics, 30000);
  } else if (adminCredits) {
    document.title = `${VerigoI18n.text("棰濆害绠＄悊")} | Verigo`;
    if (window.location.pathname !== "/admin/credits") window.history.pushState({}, "", "/admin/credits");
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
    loadAdminAccounts();
    loadAdminFeatureUsage();
  } else if (wallet) {
    document.title = `${VerigoI18n.text("璧勯噾涓庝娇鐢?)} | Verigo`;
    if (window.location.pathname !== "/wallet") window.history.pushState({}, "", "/wallet");
    loadWallet();
  } else if (history) {
    document.title = `${VerigoI18n.text("鍘嗗彶璁板綍")} | Verigo`;
    if (window.location.pathname !== "/history") window.history.pushState({}, "", "/history");
    loadHistoryPage();
  } else if (workspace) {
    document.title = "Workspace | Verigo";
    if (window.location.pathname !== "/workspace") window.history.pushState({}, "", "/workspace");
    loadWorkspaceHome();
  } else if (lists) {
    document.title = `${VerigoI18n.text("鎴戠殑鍒楄〃")} | Verigo`;
    if (window.location.pathname === "/lists" || !window.location.pathname.startsWith("/lists/")) window.history.pushState({}, "", "/lists");
    loadListsPage();
  } else {
    document.title = "Verigo";
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
    if (["/dashboard", "/admin/credits", "/wallet", "/history", "/workspace", "/lists"].includes(window.location.pathname)) window.history.replaceState({}, "", "/");
  }
  updateCount();
}

window.addEventListener("popstate", () => {
  const pathView = { "/workspace": "workspace", "/lists": "lists", "/dashboard": "dashboard", "/admin/credits": "admin-credits", "/wallet": "wallet", "/history": "history" }[window.location.pathname] || (window.location.pathname.startsWith("/lists/") ? "lists" : "single");
  switchView(pathView);
});

function formatMoney(fen) {
  return `楼${(Number(fen || 0) / 100).toFixed(2)}`;
}

function setMetric(id, value) {
  el(id).textContent = Number(value || 0).toLocaleString("zh-CN");
}

function formatDuration(seconds) {
  const total = Math.round(Number(seconds || 0));
  if (total < 60) return `${total} 绉抈;
  return `${Math.floor(total / 60)} 鍒?${total % 60} 绉抈;
}

function renderTraffic(days) {
  const chart = el("dashboard-traffic-chart");
  const width = 760;
  const height = 270;
  const padding = { top: 18, right: 16, bottom: 34, left: 38 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const series = [
    { key: "unique_visitors", color: "#1a73e8", label: "鐙珛璁垮" },
    { key: "engaged_sessions", color: "#34a853", label: "浜掑姩浼氳瘽" },
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
      return `<circle cx="${x}" cy="${y}" r="3" fill="${itemSeries.color}"><title>${item.day} ${itemSeries.label}锛?{Number(item[itemSeries.key] || 0).toLocaleString("zh-CN")}</title></circle>`;
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
    el("metric-report-engagement-rate").textContent = `浜掑姩鐜?${engagementRate.toFixed(1)}%`;
    setMetric("metric-report-submissions", submissions);
    el("metric-report-submission-rate").textContent = `浼氳瘽杞寲 ${submissionRate.toFixed(1)}%`;
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
    el("metric-audience-engagement-rate").textContent = `浜掑姩鐜?${engagementRate.toFixed(1)}%`;
    el("metric-today-revenue").textContent = formatMoney(today.revenue_fen);
    el("metric-today-orders").textContent = `${Number(today.paid_orders || 0).toLocaleString("zh-CN")} 绗斿凡鏀粯璁㈠崟`;
    el("metric-total-revenue").textContent = formatMoney(data.totals.revenue_fen);
    setMetric("metric-total-paid-orders", data.totals.paid_orders);
    const averageOrderFen = Number(data.totals.paid_orders || 0) ? Number(data.totals.revenue_fen || 0) / Number(data.totals.paid_orders) : 0;
    el("metric-average-order-value").textContent = formatMoney(averageOrderFen);
    ["queued", "running", "failed"].forEach((status) => setMetric(`metric-jobs-${status}`, data.jobs[status]));
    renderTraffic(data.daily);
    el("dashboard-updated").textContent = `鏈€杩戞洿鏂帮細${new Date(data.updated_at).toLocaleString("zh-CN")}`;
  } catch (error) {
    el("dashboard-updated").textContent = `鏁版嵁鍔犺浇澶辫触锛?{error.message}`;
  }
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});

document.querySelectorAll("[data-commercial-view]").forEach((link) => {
  link.addEventListener("click", () => switchView(link.dataset.commercialView));
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
  el("file-title").textContent = "姝ｅ湪瑙ｆ瀽鈥?;
  el("file-meta").textContent = file.name;
  errorBox.textContent = "";
  const form = new FormData();
  form.append("file", file);
  try {
    const payload = await api("/api/import", { method: "POST", body: form });
    state.fileEmails = payload.emails;
    el("file-title").textContent = file.name;
    el("file-meta").textContent = `${payload.count.toLocaleString()} 涓偖绠盽;
  } catch (error) {
    el("file-title").textContent = "閫夋嫨鏂囦欢";
    el("file-meta").textContent = "TXT 路 CSV 路 JSON 路 XLSX 路 XLSM 路 XLS";
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
    errorBox.textContent = state.view === "single" ? "璇疯緭鍏ヤ竴涓偖绠卞湴鍧€" : "璇疯嚦灏戣緭鍏ヤ竴涓偖绠卞湴鍧€";
    return;
  }
  if (state.view === "single" && emails.length !== 1) {
    errorBox.textContent = "鍗曚釜楠岃瘉涓€娆″彧鑳芥彁浜や竴涓偖绠卞湴鍧€";
    return;
  }
  startButton.disabled = true;
  startButton.textContent = "姝ｅ湪鎻愪氦鈥?;
  try {
    state.guestToken = null;
    const isFreeSingle = state.view === "single";
    // Keep the existing API field while choosing capacity internally.
    const workerCount = isFreeSingle ? 1 : 4;
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
    ? ["QQ 涓撳睘浣庡苟鍙?, "mode-qq"]
    : (modeLabels[job.worker_count] || ["鑷畾涔夋ā寮?, "mode-standard"]);
  mode.textContent = VerigoI18n.text(modeLabel);
  mode.className = `mode-badge ${modeClass}`;
  const isActive = job.status === "queued" || job.status === "running";
  el("stop-job-button").classList.toggle("hidden", !isActive);
  el("stop-job-button").disabled = !isActive;
  el("resume-job-button").classList.toggle("hidden", job.status !== "stopped");
  el("resume-job-button").disabled = job.status !== "stopped";
  el("progress-percent").textContent = `${job.progress}%`;
  el("progress-bar").style.width = `${job.progress}%`;
  const progressCopy = job.error
    || (job.status === "queued" && job.queue_position ? `鎺掗槦涓紝鍓嶆柟杩樻湁 ${job.queue_position - 1} 涓换鍔 : `${job.completed} / ${job.total} 宸插鐞哷);
  renderJobProgress(job, progressCopy);
  if (job.summary) renderSummary(job.summary);
  el("download-button").disabled = !job.download_url;
}

function renderJobProgress(job, progressCopy) {
  clearInterval(state.retryCountdownTimer);
  const suffix = job.qq_slow ? "锛決Q 閭閲囩敤浣庡苟鍙戝拰鑷姩閫€閬跨瓥鐣ワ紝璇疯€愬績绛夊緟銆? : "";
  const retryAt = job.retry_at ? new Date(job.retry_at) : null;
  const render = () => {
    if (!retryAt || Number.isNaN(retryAt.getTime())) {
      el("progress-copy").textContent = VerigoI18n.text(`${progressCopy}${suffix}`);
      return;
    }
    const seconds = Math.max(0, Math.ceil((retryAt.getTime() - Date.now()) / 1000));
    const countdown = seconds >= 60
      ? `${Math.floor(seconds / 60)} 鍒?${seconds % 60} 绉抈
      : `${seconds} 绉抈;
    el("progress-copy").textContent = VerigoI18n.text(`${progressCopy}锛?{countdown} 鍚庡啀娆″鏍?{suffix}`);
  };
  render();
  if (retryAt && retryAt.getTime() > Date.now()) {
    state.retryCountdownTimer = window.setInterval(() => { render(); renderResults(); }, 1000);
  }
}

function formatJobName(timestamp) {
  const label = VerigoI18n.locale === "en" ? "Email verification" : "閭楠岃瘉";
  if (!timestamp) return label;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return label;
  return `${label} ${new Intl.DateTimeFormat(VerigoI18n.locale === "en" ? "en-US" : "zh-CN", {
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
      if (job.status === "completed" && job.retry_at) {
        schedulePoll(2000);
      } else {
        clearInterval(state.retryCountdownTimer);
      }
    } else if (job.status !== "failed") {
      schedulePoll();
    }
  } catch (error) {
    // Recent jobs are a background enhancement. A stale session (or a test
    // fixture that only mocks /auth/me) must not overwrite the active job UI.
    if (!/sign in|鐧诲綍|閻ц缍?i.test(error.message || "")) errorBox.textContent = error.message;
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
  if (item.progress_state === "pending") return ["绛夊緟楠岃瘉", "result-pending", "pending"];
  if (item.progress_state === "verifying") return ["楠岃瘉涓?, "result-running", "verifying"];
  if (item.progress_state === "failed") return ["鏈畬鎴?, "result-failed", "failed"];
  if (item.skipped) return ["宸插仠姝?, "result-skipped", "skipped"];
  if (item.deliverable === true) return ["鍙姇閫?, "result-good", "deliverable"];
  if (item.deliverable === false) return ["涓嶅彲鎶曢€?, "result-bad", "undeliverable"];
  return ["寰呯‘璁?, "result-unknown", "unknown"];
}

function renderResults() {
  const body = el("results-body");
  const rows = state.results;
  ["copy-deliverable-button", "copy-undeliverable-button", "copy-unknown-button", "copy-all-button"].forEach((id) => { if (el(id)) el(id).disabled = !state.resultsAvailable; });
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    row.className = "empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = state.results.length ? "娌℃湁绗﹀悎鏉′欢鐨勭粨鏋? : "姝ｅ湪绛夊緟棣栨潯楠岃瘉缁撴灉";
    row.append(cell);
    body.append(row);
    renderPagination();
    return;
  }
  rows.forEach((item) => {
    const [label, className] = resultMeta(item);
    const row = document.createElement("tr");
    const values = [
      item.email,
      label,
      VerigoI18n.resultValue(item.verification_method || item.strategy || "-"),
      VerigoI18n.resultValue(item.smtp_result || item.message || "-"),
      "璇︽儏",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 0) {
        cell.textContent = item.email;
        if (item.retry_updated) {
          const dot = document.createElement("i"); dot.className = "result-email-update";
          dot.title = VerigoI18n.text("璇ラ偖绠辩殑澶嶆牳缁撴灉宸叉洿鏂?); cell.append(dot);
          row.addEventListener("click", async () => {
            await api(`/api/jobs/${state.jobId}/results/${item.original_index}/reviewed`, { method: "POST" });
            item.retry_updated = false; renderResults(); await loadRecentJobs();
          }, { once: true });
        }
      } else if (index === 1) {
        const pill = document.createElement("span");
        pill.className = `result-pill ${className}`;
        pill.textContent = label;
        cell.append(pill);
      } else if (index === 4) {
        const action = document.createElement("button");
        action.type = "button";
        action.className = "text-action result-detail-action";
        action.textContent = "鏌ョ湅璇︽儏";
        action.addEventListener("click", () => openResultDetails(item));
        cell.append(action);
      } else {
        cell.className = "detail-cell";
        cell.textContent = value;
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
  el("results-page-info").textContent = available ? `宸叉樉绀?${start}-${end}锛屽叡 ${available} 涓偖绠盽 : "绛夊緟楠岃瘉缁撴灉";
  el("previous-page").disabled = state.page === 0;
  el("next-page").disabled = (state.page + 1) * pageSize >= available;
  renderPageNumbers(el("results-pages"), state.page, Math.max(1, Math.ceil(available / pageSize)), async (page) => {
    state.page = page; await loadResults();
  });
}

function openResultDetails(item) {
  state.activeResultItem = item;
  const drawer = el("result-detail-drawer");
  el("result-detail-title").textContent = item.email || "閭璇︽儏";
  const fields = [
    ["鐘舵€?, resultMeta(item)[0]],
    ["鍩熷悕绫诲瀷", item.domain_type || "-"],
    ["楠岃瘉鏂瑰紡", VerigoI18n.resultValue(item.verification_method || item.strategy || "-")],
    ["鏈嶅姟鍣ㄥ搷搴?, VerigoI18n.resultValue(item.smtp_result || item.message || "-")],
  ];
  const content = el("result-detail-content");
  content.replaceChildren();
  fields.forEach(([label, value]) => {
    const row = document.createElement("div"); row.className = "detail-field";
    const key = document.createElement("span"); key.textContent = label;
    const val = document.createElement("strong"); val.textContent = value;
    row.append(key, val); content.append(row);
  });
  el("result-history-button").disabled = !state.user || !(item.saved_result_id || item.result_id);
  drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false");
}

function closeResultDetails() {
  const drawer = el("result-detail-drawer");
  drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true");
}

async function openSaveResultDialog(item = state.activeResultItem) {
  if (!item || !state.jobId) return;
  if (!state.user) {
    state.pendingSaveResult = { jobId: state.jobId, guestToken: state.guestToken, resultIndex: Number(item.original_index ?? item.result_index ?? 0), email: item.email };
    el("auth-dialog").showModal(); setAuthMode("login");
    el("auth-error").textContent = VerigoI18n.text("璇峰厛鐧诲綍锛岀櫥褰曞悗浼氱户缁繚瀛樺綋鍓嶇粨鏋?);
    return;
  }
  const select = el("save-result-list-select");
  el("save-result-error").textContent = ""; el("save-result-new-list-name").value = "";
  select.replaceChildren(new Option(VerigoI18n.text("鏂板缓鍒楄〃"), ""));
  try {
    const lists = await api("/api/lists");
    lists.forEach((list) => select.append(new Option(`${list.name} (${list.result_count || 0})`, list.id)));
    el("save-result-dialog").showModal();
  } catch (requestError) { el("save-result-error").textContent = requestError.message; }
}

async function submitSaveResult() {
  const item = state.activeResultItem || {};
  const payload = { job_id: state.jobId, result_index: Number(item.original_index ?? item.result_index ?? 0) };
  const selected = el("save-result-list-select").value; const newName = el("save-result-new-list-name").value.trim();
  if (selected) payload.list_id = selected; else if (newName) payload.list_name = newName; else throw new Error(VerigoI18n.text("璇烽€夋嫨鍒楄〃鎴栬緭鍏ユ柊鍒楄〃鍚嶇О"));
  const response = await api("/api/results/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (response.result?.id) item.saved_result_id = response.result.id;
  el("save-result-dialog").close(); el("result-history-button").disabled = !state.user || !item.saved_result_id;
  errorBox.textContent = VerigoI18n.text(response.added ? "缁撴灉宸蹭繚瀛樺埌鍒楄〃" : "缁撴灉宸插湪鍒楄〃涓?);
}

async function copyResultFields() {
  const item = state.activeResultItem; if (!item) return;
  const text = [`email: ${item.email || ""}`, `status: ${resultMeta(item)[2]}`, `verification_method: ${item.verification_method || item.strategy || ""}`, `server_response: ${item.smtp_result || item.message || ""}`].join("\n");
  try { await navigator.clipboard.writeText(text); errorBox.textContent = VerigoI18n.text("鍏抽敭瀛楁宸插鍒?); } catch (_) { errorBox.textContent = VerigoI18n.text("澶嶅埗澶辫触锛岃鎵嬪姩閫夋嫨"); }
}

async function openResultHistory() {
  const id = state.activeResultItem?.saved_result_id || state.activeResultItem?.result_id; if (!id || !state.user) return;
  try {
    el("result-detail-title").textContent = state.activeResultItem.email || VerigoI18n.text("缁撴灉鍘嗗彶");
    el("result-detail-drawer").classList.add("open"); el("result-detail-drawer").setAttribute("aria-hidden", "false");
    const data = await api(`/api/results/${encodeURIComponent(id)}/history`); const content = el("result-detail-content");
    const heading = document.createElement("p"); heading.className = "detail-history-heading"; heading.textContent = `${data.email} 路 ${data.items.length} 涓増鏈琡; content.prepend(heading);
    data.items.forEach((version) => { const row = document.createElement("div"); row.className = "detail-field"; const key = document.createElement("span"); key.textContent = `${VerigoI18n.text(version.status)} 路 ${VerigoI18n.formatDate(version.created_at)}`; const val = document.createElement("strong"); val.textContent = version.verification_method || "-"; row.append(key, val); content.append(row); });
  } catch (requestError) { errorBox.textContent = requestError.message; }
}

async function reverifyActiveResult() {
  const item = state.activeResultItem; if (!item || !state.user || !item.email) return;
  try {
    const job = item.saved_result_id
      ? await api(`/api/results/${encodeURIComponent(item.saved_result_id)}/reverify`, { method: "POST" })
      : await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ emails: [item.email], worker_count: 1 }) });
    closeResultDetails(); state.jobId = job.id; state.guestToken = null; sessionStorage.setItem("verigo_job_id", job.id); state.page = 0; state.results = []; switchView("single"); showJob(job); await loadAccount(); schedulePoll(300);
  } catch (requestError) { errorBox.textContent = requestError.message; }
}

async function copyEmails(kind = "all") {
  if (!state.jobId) return;
  try {
    const items = [];
    const limit = 100;
    for (let offset = 0; offset < Math.max(state.resultsAvailable, 1); offset += limit) {
      const data = await api(`/api/jobs/${state.jobId}/results?limit=${limit}&offset=${offset}&deliverability=all`);
      items.push(...(data.items || []));
      if (!data.items?.length || items.length >= Number(data.available || 0)) break;
    }
    const selected = kind === "deliverable" ? items.filter((item) => item.deliverable === true)
      : kind === "undeliverable" ? items.filter((item) => item.deliverable === false)
        : kind === "unknown" ? items.filter((item) => item.deliverable == null) : items;
    const text = selected.map((item) => item.email).join("\n");
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else { const area = document.createElement("textarea"); area.value = text; document.body.append(area); area.select(); document.execCommand("copy"); area.remove(); }
    errorBox.textContent = `宸插鍒?${selected.length} 涓偖绠盽;
  } catch (error) { errorBox.textContent = error.message; }
}

el("close-result-detail")?.addEventListener("click", closeResultDetails);
el("result-save-button")?.addEventListener("click", () => openSaveResultDialog());
el("result-copy-button")?.addEventListener("click", copyResultFields);
el("result-history-button")?.addEventListener("click", openResultHistory);
el("result-reverify-button")?.addEventListener("click", reverifyActiveResult);
el("close-save-result")?.addEventListener("click", () => el("save-result-dialog").close());
el("save-result-form")?.addEventListener("submit", async (event) => { event.preventDefault(); const button = el("save-result-submit"); button.disabled = true; el("save-result-error").textContent = ""; try { await submitSaveResult(); } catch (requestError) { el("save-result-error").textContent = requestError.message; } finally { button.disabled = false; } });
el("copy-deliverable-button")?.addEventListener("click", () => copyEmails("deliverable"));
el("copy-undeliverable-button")?.addEventListener("click", () => copyEmails("undeliverable"));
el("copy-unknown-button")?.addEventListener("click", () => copyEmails("unknown"));
el("copy-all-button")?.addEventListener("click", () => copyEmails("all"));

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
    if (!response.ok) throw new Error("涓嬭浇澶辫触");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = state.downloadName || "Verigo-閭楠岃瘉缁撴灉.csv";
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
  state.discoverySelection ||= new Set();
  el("discovery-save-selected") && (el("discovery-save-selected").disabled = !state.discoverySelection.size);
  const appendSaveAction = (row, item) => {
    const actionCell = document.createElement("td");
    const save = document.createElement("button"); save.type = "button"; save.className = "text-action"; save.textContent = VerigoI18n.text("淇濆瓨鍒板垪琛?);
    save.addEventListener("click", () => { if (!state.discovery.jobId) { el("discovery-error").textContent = VerigoI18n.text("璇峰厛楠岃瘉鍊欓€夐偖绠憋紝瀹屾垚鍚庡嵆鍙繚瀛樼粨鏋?); return; } state.jobId = state.discovery.jobId; openResultDetails(item); openSaveResultDialog(item); });
    actionCell.append(save); row.append(actionCell);
  };
  if (!state.discovery.results.length) {
    if (state.discovery.candidates.length && !state.discovery.jobId) {
      state.discovery.candidates.forEach((email) => {
        const row = document.createElement("tr");
        [email, "鏈獙璇?, "-", "-"].forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        });
        const checkCell = row.firstElementChild; const check = document.createElement("input"); check.type = "checkbox"; check.addEventListener("change", () => { const index = state.discovery.candidates.indexOf(email); if (check.checked) state.discoverySelection.add(index); else state.discoverySelection.delete(index); el("discovery-save-selected").disabled = !state.discoverySelection.size; }); checkCell.append(check); appendSaveAction(row, { email, original_index: state.discovery.candidates.indexOf(email) }); body.append(row);
      });
    } else {
      const row = document.createElement("tr");
      row.className = "empty-row";
      row.innerHTML = '<td colspan="6">正在生成验证结果</td>';
      body.append(row);
    }
    return;
  }
  state.discovery.results.forEach((item) => {
    const [label, className] = resultMeta(item);
    const row = document.createElement("tr");
    const selectCell = document.createElement("td"); const check = document.createElement("input"); check.type = "checkbox"; check.addEventListener("change", () => { if (check.checked) state.discoverySelection.add(item.original_index); else state.discoverySelection.delete(item.original_index); el("discovery-save-selected").disabled = !state.discoverySelection.size; }); selectCell.append(check); row.append(selectCell);
    [
      item.email,
      label,
      VerigoI18n.resultValue(item.verification_method || item.strategy || "-"),
      VerigoI18n.resultValue(item.smtp_result || item.message || "-"),
    ].forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 1) {
        const pill = document.createElement("span");
        pill.className = `result-pill ${className}`;
        pill.textContent = value;
        cell.append(pill);
      } else cell.textContent = String(value);
      row.append(cell);
    });
    appendSaveAction(row, item); body.append(row);
  });
}

function showDiscoveryJob(job) {
  el("discovery-title").textContent = VerigoI18n.text(`鏌ユ壘 ${job.total} 涓€欓€夐偖绠盽);
  const status = el("discovery-status");
  status.textContent = statusLabels[job.status] || job.status;
  status.className = `status status-${job.status}`;
  const isActive = job.status === "queued" || job.status === "running";
  el("discovery-stop-button").classList.toggle("hidden", !isActive);
  el("discovery-stop-button").disabled = !isActive;
  el("discovery-progress-percent").textContent = `${job.progress}%`;
  el("discovery-progress-bar").style.width = `${job.progress}%`;
  const progressCopy = job.status === "queued" && job.queue_position
    ? `鎺掗槦涓紝鍓嶆柟杩樻湁 ${job.queue_position - 1} 涓换鍔
    : `${job.completed} / ${job.total} 宸插鐞哷;
  el("discovery-progress-copy").textContent = VerigoI18n.text(job.qq_slow
    ? `${progressCopy}锛決Q 閭閲囩敤浣庡苟鍙戝拰鑷姩閫€閬跨瓥鐣ワ紝璇疯€愬績绛夊緟銆俙
    : progressCopy);
}

function updateDiscoveryVerdict(job) {
  const verdict = el("discovery-verdict");
  if (job.status === "stopped") {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = VerigoI18n.text("楠岃瘉宸插仠姝紝宸蹭繚鐣欏綋鍓嶇粨鏋溿€?);
    return;
  }
  if (job.status !== "completed") {
    verdict.className = "discovery-verdict";
    verdict.textContent = VerigoI18n.text("姝ｅ湪浠庡€欓€夊湴鍧€涓‘璁ょ粨鏋?);
    return;
  }
  const good = state.discovery.results.filter((item) => resultType(item) === "deliverable");
  const unknown = state.discovery.results.filter((item) => resultType(item) === "unknown");
  if (good.length === 1) {
    verdict.className = "discovery-verdict good";
    verdict.textContent = VerigoI18n.text(`宸叉壘鍒板敮涓€鍙‘璁ら偖绠憋細${good[0].email}`);
  } else if (good.length > 1) {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = VerigoI18n.text(`鎵惧埌 ${good.length} 涓彲纭鍦板潃锛岃缁撳悎鑱屼綅鎴栧叕寮€淇℃伅杩涗竴姝ョ‘璁ゃ€俙);
  } else if (unknown.length) {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = VerigoI18n.text("娌℃湁鍙‘璁ゅ湴鍧€锛岄儴鍒嗗€欓€夋殏鏃舵棤娉曠‘璁ゃ€傝绋嶅悗閲嶈瘯鎴栨鏌ュ煙鍚嶃€?);
  } else {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = VerigoI18n.text("鏈壘鍒板彲纭鍦板潃銆傝妫€鏌ュ鍚嶅拰鍩熷悕锛屾垨瀵规柟鍙兘宸茬鑱屻€?);
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
    verifyButton.textContent = VerigoI18n.text(`鍏嶈垂楠岃瘉鍊欓€夐偖绠?路 ${state.discovery.candidates.length} 涓湴鍧€`);
    el("discovery-title").textContent = VerigoI18n.text(`${state.discovery.candidates.length} 涓€欓€夐偖绠盽);
    el("discovery-status").textContent = VerigoI18n.text("宸叉壘鍒?);
    el("discovery-status").className = "status status-completed";
    el("discovery-progress-percent").textContent = "0%";
    el("discovery-progress-bar").style.width = "0%";
    el("discovery-progress-copy").textContent = VerigoI18n.text("绛夊緟楠岃瘉");
    const hasQqCandidate = state.discovery.candidates.some(isQqEmail);
    el("discovery-verdict").className = hasQqCandidate ? "discovery-verdict warn" : "discovery-verdict";
    el("discovery-verdict").textContent = VerigoI18n.text(hasQqCandidate
      ? `宸茬敓鎴?${state.discovery.candidates.length} 涓€欓€夊湴鍧€銆俀Q 閭楠岃瘉閲囩敤涓撳睘浣庡苟鍙戠瓥鐣ワ紝楠岃瘉閫熷害杈冩參锛岃鑰愬績绛夊緟銆俙
      : `宸茬敓鎴?${state.discovery.candidates.length} 涓€欓€夊湴鍧€`);
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
el("discovery-save-selected")?.addEventListener("click", async () => {
  if (!state.discovery.jobId || !state.discoverySelection.size) return;
  try {
    const lists = await api("/api/lists");
    const listId = lists[0]?.id || null;
    if (!listId) { el("discovery-error").textContent = VerigoI18n.text("璇峰厛鍒涘缓涓€涓垪琛?); switchView("lists"); return; }
    const response = await api("/api/results/save-batch", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ job_id: state.discovery.jobId, result_indices:[...state.discoverySelection], list_id:listId }) });
    el("discovery-error").textContent = `${VerigoI18n.text("宸蹭繚瀛?)} ${response.results?.length || 0} ${VerigoI18n.text("涓粨鏋?)}`;
  } catch (error) { el("discovery-error").textContent = error.message; }
});
el("discovery-export-button")?.addEventListener("click", () => { if (!state.discovery.jobId) return; window.location.href = `/api/jobs/${encodeURIComponent(state.discovery.jobId)}/download`; });
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
    const { items: jobs, total } = await api(
      `/api/jobs?offset=${state.recentJobs.offset}&limit=${state.recentJobs.limit}`
    );
    state.recentJobs.total = total;
    const container = el("recent-jobs");
    container.replaceChildren();
    if (!jobs.length) {
      const empty = document.createElement("small");
      empty.textContent = VerigoI18n.text("鏆傛棤浠诲姟");
      container.append(empty);
      el("recent-jobs-pagination").classList.add("hidden");
      return;
    }
    jobs.forEach((job) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "recent-job";
      const name = document.createElement("span");
      name.textContent = formatJobName(job.finished_at || job.started_at || job.created_at);
      const meta = document.createElement("small");
      meta.textContent = `${VerigoI18n.text(statusLabels[job.status] || job.status)} 路 ${job.total}`;
      button.append(name);
      if (job.review_updated) {
        const dot = document.createElement("i");
        dot.className = "recent-job-update";
        dot.setAttribute("aria-label", VerigoI18n.text("澶嶆牳缁撴灉宸叉洿鏂?));
        button.append(dot);
      }
      button.append(meta);
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
        if (job.review_updated) {
          await api(`/api/jobs/${job.id}/reviewed`, { method: "POST" });
          await loadRecentJobs();
        }
        if (job.status !== "completed" && job.status !== "failed") schedulePoll(300);
      });
      container.append(button);
    });
    const pager = el("recent-jobs-pagination");
    pager.classList.toggle("hidden", total <= state.recentJobs.limit);
    const page = Math.floor(state.recentJobs.offset / state.recentJobs.limit);
    el("recent-jobs-page-info").textContent = `绗?${page + 1} / ${Math.max(1, Math.ceil(total / state.recentJobs.limit))} 椤碉紝鍏?${total} 涓换鍔;
    renderPageNumbers(el("recent-jobs-pages"), page, Math.max(1, Math.ceil(total / state.recentJobs.limit)), async (nextPage) => {
      state.recentJobs.offset = nextPage * state.recentJobs.limit; await loadRecentJobs();
    });
  } catch (error) {
    errorBox.textContent = error.message;
  }
}

async function loadHistoryPage() {
  if (!state.user || state.view !== "history") return;
  const search = encodeURIComponent((el("history-search")?.value || "").trim());
  const status = encodeURIComponent(el("history-filter")?.value || "all");
  try {
    const data = await api(`/api/jobs?offset=${state.history.offset}&limit=${state.history.limit}&search=${search}&status=${status}`);
    state.history.total = data.total || 0;
    const list = el("history-list"); list.replaceChildren();
    (data.items || []).forEach((job) => {
      const button = document.createElement("button"); button.type = "button"; button.className = "history-item";
      const name = document.createElement("strong"); name.textContent = job.download_name || job.file_name || formatJobName(job.created_at);
      const meta = document.createElement("span"); meta.textContent = `${statusLabels[job.status] || job.status} 路 ${job.total || 0} 涓偖绠盽;
      button.append(name, meta); button.addEventListener("click", () => { switchView("single"); showJob(job); state.results = []; state.page = 0; loadResults(); }); list.append(button);
    });
    if (!(data.items || []).length) { const empty = document.createElement("p"); empty.className = "history-empty"; empty.textContent = "鏆傛棤鍘嗗彶浠诲姟"; list.append(empty); }
    const page = Math.floor(state.history.offset / state.history.limit); const pages = Math.max(1, Math.ceil(state.history.total / state.history.limit));
    el("history-page-info").textContent = `${page + 1} / ${pages}`; el("history-prev").disabled = page === 0; el("history-next").disabled = page + 1 >= pages;
    renderPageNumbers(el("history-pages"), page, pages, async (next) => { state.history.offset = next * state.history.limit; await loadHistoryPage(); });
  } catch (error) { errorBox.textContent = error.message; }
}

el("history-refresh")?.addEventListener("click", loadHistoryPage);
el("history-search")?.addEventListener("input", () => { state.history.offset = 0; clearTimeout(state.historyTimer); state.historyTimer = setTimeout(loadHistoryPage, 250); });
el("history-filter")?.addEventListener("change", () => { state.history.offset = 0; loadHistoryPage(); });
el("history-prev")?.addEventListener("click", () => { if (state.history.offset >= state.history.limit) { state.history.offset -= state.history.limit; loadHistoryPage(); } });
el("history-next")?.addEventListener("click", () => { if (state.history.offset + state.history.limit < state.history.total) { state.history.offset += state.history.limit; loadHistoryPage(); } });

function updateAccount() {
  el("account-button").textContent = state.user ? state.user.email : "鐧诲綍";
  el("account-name").textContent = state.user?.email || "";
  const trialCredits = Number(state.user?.trial_credits || 0);
  el("account-credits").textContent = state.user
    ? state.user.is_admin
      ? VerigoI18n.text("鏃犻檺棰濆害")
      : VerigoI18n.locale === "en"
        ? `${state.user.credits || 0} verifications${trialCredits ? ` 路 ${trialCredits} trial credits` : ""}`
        : `${state.user.credits || 0} 楠岃瘉娆℃暟${trialCredits ? ` 路 ${trialCredits} 浣撻獙娆℃暟` : ""}`
    : "";
  el("account-credits").title = state.user?.trial_credit_expires_at
    ? VerigoI18n.locale === "en"
      ? `Trial credits valid until ${VerigoI18n.formatDate(state.user.trial_credit_expires_at)}`
      : `浣撻獙棰濆害鏈夋晥鑷?${VerigoI18n.formatDate(state.user.trial_credit_expires_at)}`
    : "";
  el("bind-email-button").classList.toggle("hidden", !state.user?.needs_email_binding);
  el("dashboard-nav").classList.toggle("hidden", !state.user?.is_admin);
  el("admin-credits-nav").classList.toggle("hidden", !state.user?.is_admin);
  el("wallet-nav").classList.toggle("hidden", !state.user);
  el("workspace-nav").classList.toggle("hidden", !state.user);
  el("lists-nav").classList.toggle("hidden", !state.user);
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
    syncOnboarding();
  }
}

window.addEventListener("verigo:localechange", () => {
  updateAccount();
});

async function loadAccount() {
  try { state.user = await api("/api/auth/me"); } catch (_) { state.user = null; }
  updateAccount();
}

async function loadWorkspaceHome() {
  if (!state.user) return;
  const credits = Number(state.user.credits || 0) + Number(state.user.trial_credits || 0);
  el("workspace-credits").textContent = credits.toLocaleString("zh-CN");
  try {
    const data = await api("/api/workspace");
    const jobs = data.items || [];
    const locale = VerigoI18n.locale === "en" ? "en-US" : "zh-CN";
    el("workspace-job-count").textContent = Number(data.total || jobs.length).toLocaleString(locale);
    el("workspace-processed").textContent = Number(data.processed_today || 0).toLocaleString(locale);
    el("workspace-deliverable-rate").textContent = Number(data.settled || 0) ? `${Math.round(Number(data.deliverable || 0) / Number(data.settled) * 100)}%` : "鈥?;
    const list = el("workspace-recent-jobs"); list.replaceChildren();
    const recentResults = el("workspace-recent-results");
    if (recentResults) { recentResults.replaceChildren(); (data.recent_results || []).forEach((result) => { const item = document.createElement("button"); item.type = "button"; item.className = "workspace-job-row"; const name = document.createElement("strong"); name.textContent = result.email; const meta = document.createElement("span"); meta.textContent = `${VerigoI18n.text(result.status)} 路 ${VerigoI18n.formatDate(result.created_at)}`; item.append(name, meta); item.addEventListener("click", () => { state.activeResultItem = { email: result.email, saved_result_id: result.id }; openResultHistory(); }); recentResults.append(item); }); if (!recentResults.children.length) { const empty = document.createElement("p"); empty.className = "workspace-empty"; empty.textContent = VerigoI18n.text("杩樻病鏈変繚瀛樼殑缁撴灉"); recentResults.append(empty); } }
    await loadWorkspaceListsPreview();
    if (!jobs.length) { const empty = document.createElement("p"); empty.className = "workspace-empty"; empty.textContent = VerigoI18n.text("杩樻病鏈変换鍔★紝鍏堥獙璇佷竴涓偖绠卞惂銆?); list.append(empty); }
    jobs.slice(0, 5).forEach((job) => {
      const item = document.createElement("button"); item.type = "button"; item.className = "workspace-job-row";
      const name = document.createElement("strong");
      name.textContent = formatJobName(job.created_at);
      const meta = document.createElement("span");
      meta.textContent = `${VerigoI18n.text(statusLabels[job.status] || job.status)} 路 ${Number(job.total || 0).toLocaleString(locale)} ${VerigoI18n.text("涓偖绠?)}`;
      item.append(name, meta);
      item.addEventListener("click", () => { switchView("single"); showJob(job); state.results = []; state.page = 0; loadResults(); }); list.append(item);
    });
  } catch (error) { el("workspace-recent-jobs").textContent = VerigoI18n.text("浠诲姟鍔犺浇澶辫触锛岃绋嶅悗鍒锋柊銆?); }
}

async function loadWorkspaceListsPreview() {
  const container = el("workspace-lists-preview");
  if (!container || !state.user) return;
  try {
    const lists = await api("/api/lists"); container.replaceChildren();
    if (!lists.length) { const empty = document.createElement("p"); empty.className = "workspace-empty"; empty.textContent = VerigoI18n.text("杩樻病鏈変繚瀛樼殑鍒楄〃"); container.append(empty); return; }
    lists.slice(0, 4).forEach((savedList) => { const item = document.createElement("button"); item.type = "button"; item.className = "workspace-job-row"; const name = document.createElement("strong"); name.textContent = savedList.name; const meta = document.createElement("span"); meta.textContent = `${savedList.result_count || 0} ${VerigoI18n.text("涓粨鏋?)}`; item.append(name, meta); item.addEventListener("click", () => { switchView("lists"); openListDetail(savedList.id); }); container.append(item); });
  } catch (error) { container.textContent = error.message; }
}

async function loadListsPage() {
  if (!state.user || state.view !== "lists") return;
  const index = el("lists-index"); const detail = el("list-detail");
  try {
    const lists = await api("/api/lists");
    index.replaceChildren(); detail.classList.add("hidden");
    if (!lists.length) { const empty = document.createElement("div"); empty.className = "workspace-card list-empty"; empty.textContent = VerigoI18n.text("杩樻病鏈夊垪琛紝鍏堜繚瀛樹竴涓獙璇佺粨鏋滃惂銆?); index.append(empty); return; }
    lists.forEach((list) => {
      const card = document.createElement("article"); card.className = "list-card";
      const title = document.createElement("h2"); title.textContent = list.name;
      const meta = document.createElement("p"); meta.textContent = `${Number(list.result_count || 0).toLocaleString(VerigoI18n.locale === "en" ? "en-US" : "zh-CN")} ${VerigoI18n.text("涓粨鏋?)} 路 ${VerigoI18n.formatDate(list.updated_at)}`;
      const open = document.createElement("button"); open.type = "button"; open.className = "text-action"; open.textContent = VerigoI18n.text("鎵撳紑鍒楄〃"); open.addEventListener("click", () => openListDetail(list.id));
      card.append(title, meta, open); index.append(card);
    });
  } catch (error) { index.textContent = error.message; }
}

async function openListDetail(listId, status = el("list-status-filter")?.value || "all") {
  try {
    state.listSelection = new Set();
    el("list-select-all") && (el("list-select-all").checked = false);
    el("list-reverify-button") && (el("list-reverify-button").disabled = true);
    const data = await api(`/api/lists/${encodeURIComponent(listId)}?status=${encodeURIComponent(status)}`); const list = data.list;
    el("lists-index").classList.add("hidden"); el("list-detail").classList.remove("hidden");
    el("list-detail-title").textContent = list.name; el("list-detail-meta").textContent = `${Number(data.total || 0).toLocaleString(VerigoI18n.locale === "en" ? "en-US" : "zh-CN")} ${VerigoI18n.text("涓粨鏋?)}`;
    const items = el("list-detail-items"); items.replaceChildren();
    if (!data.items.length) { const empty = document.createElement("p"); empty.className = "list-empty"; empty.textContent = VerigoI18n.text("鍒楄〃涓繕娌℃湁缁撴灉"); items.append(empty); }
    data.items.forEach((item) => { const row = document.createElement("div"); row.className = "list-result-row"; const check = document.createElement("input"); check.type = "checkbox"; check.value = item.id; check.addEventListener("change", () => { if (check.checked) state.listSelection.add(item.id); else state.listSelection.delete(item.id); el("list-reverify-button").disabled = !state.listSelection.size; }); const info = document.createElement("div"); const email = document.createElement("strong"); email.textContent = item.email; const meta = document.createElement("span"); meta.textContent = `${VerigoI18n.text(item.status)} 路 ${VerigoI18n.text(item.source)}`; info.append(email, meta); const remove = document.createElement("button"); remove.type = "button"; remove.className = "text-action"; remove.textContent = VerigoI18n.text("绉婚櫎"); remove.addEventListener("click", async () => { await api(`/api/lists/${listId}/results`, { method:"DELETE", headers:{"Content-Type":"application/json"}, body:JSON.stringify({result_ids:[item.id]}) }); await openListDetail(listId, status); }); row.append(check, info, remove); items.append(row); });
    el("list-export-button").onclick = () => { window.location.href = `/api/lists/${encodeURIComponent(listId)}/export`; };
    window.history.pushState({}, "", `/lists/${encodeURIComponent(listId)}`);
  } catch (error) { el("list-detail-items").textContent = error.message; }
}

el("create-list-button")?.addEventListener("click", async () => { const name = window.prompt(VerigoI18n.text("璇疯緭鍏ュ垪琛ㄥ悕绉?)); if (!name?.trim()) return; await api("/api/lists", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:name.trim()}) }); await loadListsPage(); });
el("list-back-button")?.addEventListener("click", () => { window.history.pushState({}, "", "/lists"); loadListsPage(); });
el("list-status-filter")?.addEventListener("change", () => { const id = window.location.pathname.split("/").pop(); if (id) openListDetail(id, el("list-status-filter").value); });
el("list-select-all")?.addEventListener("change", (event) => { document.querySelectorAll("#list-detail-items input[type=checkbox]").forEach((check) => { check.checked = event.target.checked; check.dispatchEvent(new Event("change")); }); });
el("list-reverify-button")?.addEventListener("click", async () => { const id = window.location.pathname.split("/").pop(); if (!id || !state.listSelection.size) return; const button = el("list-reverify-button"); button.disabled = true; try { const job = await api(`/api/lists/${encodeURIComponent(id)}/reverify`, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ result_ids:[...state.listSelection] }) }); switchView("single"); state.jobId = job.id; showJob(job); schedulePoll(300); } catch (error) { el("list-detail-meta").textContent = error.message; button.disabled = false; } });

el("dashboard-refresh").addEventListener("click", loadDashboardMetrics);
async function loadWallet() { const data = await api("/api/wallet"); const set=(id,v)=>el(id).textContent=Number(v||0).toLocaleString("zh-CN"); set("wallet-available",data.available_verifications); el("wallet-paid").textContent=`${Number(data.paid_verifications||0).toLocaleString("zh-CN")} 娆; el("wallet-used").textContent=`${Number(data.paid_verifications_used||0).toLocaleString("zh-CN")} 娆; el("wallet-recharged").textContent=`楼${(Number(data.cumulative_recharge_fen||0)/100).toFixed(2)}`; el("wallet-value").textContent=`楼${Number(data.remaining_paid_value_yuan||0).toFixed(2)}`; el("wallet-spent").textContent=`楼${Number(data.paid_used_value_yuan||0).toFixed(2)}`; el("wallet-price").textContent=`100 娆?楼${(data.price_fen_per_100/100).toFixed(2)}`; el("wallet-trial-note").textContent=data.trial_verifications?`鍙︽湁 ${data.trial_verifications} 浣撻獙娆℃暟`:"涓嶅惈浣撻獙娆℃暟"; el("wallet-updated").textContent=`鏇存柊浜?${new Date().toLocaleString("zh-CN")}`; const days=data.usage_daily||[]; const max=Math.max(1,...days.map(x=>x.verifications)); el("wallet-usage-chart").innerHTML=days.map(x=>`<div class="wallet-bar" style="height:${Math.max(4,x.verifications/max*180)}px"><span>${x.verifications}</span></div>`).join(""); el("wallet-transactions").innerHTML=(data.transactions||[]).map(x=>`<div class="wallet-transaction"><div><strong>${x.title}</strong><small>${x.credits>0?"+":""}${x.credits} 娆?${x.note||""}</small></div><div><strong>${x.amount_fen==null?"鈥?:`${x.credits<0?"-":"+"}楼${(x.amount_fen/100).toFixed(2)}`}</strong><small>${new Date(x.created_at).toLocaleString("zh-CN")}</small></div></div>`).join("")||"鏆傛棤璧勯噾娴佹按"; }
el("wallet-refresh").addEventListener("click", loadWallet);

function showOnboardingStep(step) {
  const dialog = el("onboarding-dialog");
  document.querySelectorAll("[data-onboarding-step]").forEach((section) => {
    section.classList.toggle("hidden", section.dataset.onboardingStep !== step);
  });
  if (!dialog.open) dialog.showModal();
}

async function syncOnboarding() {
  clearTimeout(state.onboardingTimer);
  const step = state.user?.onboarding_step;
  if (!step || step === "completed") return;
  // Do not interrupt the first authenticated session with a modal. The user
  // can start activation from the trial-credit action in the account area.
  if (!el("onboarding-dialog").open) return;
  showOnboardingStep(step);
  if (step !== "verification_in_progress" || !state.user.activation_job_id) return;
  try {
    const job = await api(`/api/jobs/${state.user.activation_job_id}`);
    el("onboarding-job-status").textContent = job.status === "completed"
      ? "楠岃瘉宸插畬鎴愶紝姝ｅ湪鏇存柊璐︽埛銆?
      : `姝ｅ湪楠岃瘉锛?{job.completed} / ${job.total}`;
    if (job.status === "completed") {
      state.user = await api("/api/auth/onboarding/activation/complete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: job.id }),
      });
      showOnboardingStep("completed");
      return;
    }
    if (job.status === "failed" || job.status === "stopped") {
      el("onboarding-job-status").textContent = "鏈楠岃瘉鏈畬鎴愶紝璇峰叧闂獥鍙ｅ悗閲嶆柊鎻愪氦涓€涓偖绠便€?;
      return;
    }
  } catch (error) {
    el("onboarding-job-status").textContent = error.message;
  }
  state.onboardingTimer = window.setTimeout(syncOnboarding, 1200);
}

document.querySelectorAll("[data-close-onboarding]").forEach((button) => button.addEventListener("click", () => {
  clearTimeout(state.onboardingTimer); el("onboarding-dialog").close();
}));
el("onboarding-email-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  submit.disabled = true; el("onboarding-email-error").textContent = "";
  try {
    state.user = await api("/api/auth/email-verification/confirm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: el("onboarding-email-code").value }),
    });
    showOnboardingStep("first_verification");
    el("onboarding-check-email").focus();
  } catch (error) { el("onboarding-email-error").textContent = error.message; } finally { submit.disabled = false; }
});
el("onboarding-resend").addEventListener("click", async () => {
  const button = el("onboarding-resend"); button.disabled = true; el("onboarding-email-error").textContent = "";
  try { await api("/api/auth/email-verification/request", { method: "POST" }); el("onboarding-email-error").textContent = "鏂扮殑楠岃瘉鐮佸凡鍙戦€併€?; }
  catch (error) { el("onboarding-email-error").textContent = error.message; } finally { button.disabled = false; }
});
el("onboarding-check-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  submit.disabled = true; el("onboarding-check-error").textContent = "";
  try {
    const job = await api("/api/verify/single", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: el("onboarding-check-email").value }) });
    state.jobId = job.id; state.guestToken = null; state.page = 0; state.results = []; state.resultsAvailable = 0;
    showJob(job); renderResults(); schedulePoll(300);
    state.user = await api("/api/auth/me");
    showOnboardingStep("verification_in_progress");
    syncOnboarding();
  } catch (error) { el("onboarding-check-error").textContent = error.message; } finally { submit.disabled = false; }
});
el("onboarding-go-wallet").addEventListener("click", () => { el("onboarding-dialog").close(); switchView("wallet"); });
el("onboarding-finish").addEventListener("click", () => el("onboarding-dialog").close());

function refreshPurchasePrice() {
  const packages = Math.max(1, Math.min(1000, Number(el("purchase-packages").value) || 1));
  el("purchase-packages").value = String(packages);
  el("purchase-button").textContent = `璐拱 ${(packages * 100).toLocaleString("zh-CN")} 娆?路 楼${(packages * 0.5).toFixed(2)}`;
}
el("purchase-packages").addEventListener("input", refreshPurchasePrice);
el("close-purchase").addEventListener("click", () => el("purchase-dialog").close());
el("purchase-button").addEventListener("click", async () => {
  const button = el("purchase-button"); button.disabled = true; el("purchase-status").className = "purchase-status"; el("purchase-status").textContent = "";
  try {
    const order = await api("/api/billing/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ packages: Number(el("purchase-packages").value) }) });
    el("purchase-dialog-copy").textContent = `璁㈠崟 ${order.id.slice(0, 12)} 宸插垱寤猴細${order.credits.toLocaleString("zh-CN")} 娆￠獙璇侀搴︼紝鍏?楼${(order.amount_fen / 100).toFixed(2)}銆俙;
    const actions = el("purchase-dialog-actions"); actions.replaceChildren();
    if (!order.checkout_url) throw new Error("鏀粯閫氶亾鏆傛湭閰嶇疆锛岃绋嶅悗鍐嶈瘯");
    const pay = document.createElement("a"); pay.className = "primary-action"; pay.href = order.checkout_url; pay.textContent = "鍓嶅線瀹夊叏鏀粯"; actions.append(pay);
    el("purchase-dialog-status").textContent = "鏀粯鎴愬姛鍚庯紝棰濆害浼氳嚜鍔ㄥ埌璐︺€?; el("purchase-dialog").showModal();
  } catch (error) { el("purchase-status").className = "purchase-status error"; el("purchase-status").textContent = error.message; } finally { button.disabled = false; }
});
async function loadAdminAccounts(){try{const data=await api(`/api/admin/accounts/list?offset=${state.adminAccountOffset}&limit=50`),rows=data.items,summary=data.summary||{};el("admin-metric-users").textContent=data.total.toLocaleString("zh-CN");el("admin-metric-paid").textContent=Number(summary.paid_verifications||0).toLocaleString("zh-CN");el("admin-metric-trial").textContent=Number(summary.trial_verifications||0).toLocaleString("zh-CN");el("admin-metric-used").textContent=Number(summary.used_verifications||0).toLocaleString("zh-CN");el("admin-accounts-meta").textContent=`鍏?${data.total} 涓处鎴凤紝鎸夋敞鍐屾椂闂存帓搴廯;el("admin-accounts-list").innerHTML=rows.map(r=>`<button class="admin-account-row" data-email="${r.email}" type="button"><strong>${r.email}</strong><span>浠樿垂 ${r.paid_verifications}</span><span>浣撻獙 ${r.trial_verifications}</span><span>宸茬敤 ${r.used_verifications}</span></button>`).join("")||"鏆傛棤璐︽埛";el("admin-accounts-page").textContent=`${data.offset+1}-${Math.min(data.offset+data.limit,data.total)} / ${data.total}`;el("admin-accounts-prev").disabled=!data.offset;el("admin-accounts-next").disabled=data.offset+data.limit>=data.total;const page=Math.floor(data.offset/data.limit);renderPageNumbers(el("admin-accounts-pages"),page,Math.max(1,Math.ceil(data.total/data.limit)),nextPage=>{state.adminAccountOffset=nextPage*data.limit;loadAdminAccounts();});document.querySelectorAll(".admin-account-row").forEach(b=>b.addEventListener("click",()=>{el("admin-credit-email").value=b.dataset.email;el("admin-account-lookup").click();}));}catch(error){["admin-metric-users","admin-metric-paid","admin-metric-trial","admin-metric-used"].forEach(id=>el(id).textContent="鈥?);el("admin-accounts-meta").textContent=`璐︽埛鏁版嵁鍔犺浇澶辫触锛?{error.message}`;el("admin-accounts-list").textContent="璇峰埛鏂板悗閲嶈瘯";}}
async function loadAdminFeatureUsage(){const data=await api("/api/admin/feature-usage");const days=data.daily||[];const width=620,height=350,p={top:18,right:12,bottom:30,left:30},max=Math.max(1,...days.flatMap(day=>[day.single,day.batch,day.discovery]));const x=index=>p.left+(days.length>1?index*(width-p.left-p.right)/(days.length-1):(width-p.left-p.right)/2),point=(value,index)=>`${x(index)},${p.top+(height-p.top-p.bottom)*(1-value/max)}`;const series=[["single","single"],["batch","batch"],["discovery","discovery"]];const grid=[0,.5,1].map(step=>{const y=p.top+(height-p.top-p.bottom)*step;return `<line class="admin-feature-grid" x1="${p.left}" y1="${y}" x2="${width-p.right}" y2="${y}"/><text class="admin-feature-axis" x="0" y="${y+4}">${Math.round(max*(1-step))}</text>`;}).join("");const labels=days.map((day,index)=>index%2&&days.length>8?"":`<text class="admin-feature-axis" text-anchor="middle" x="${x(index)}" y="${height-8}">${day.day.slice(5).replace("-","/")}</text>`).join("");const lines=series.map(([key,name])=>`<polyline class="admin-feature-line-${name}" points="${days.map((day,index)=>point(day[key],index)).join(" ")}" fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>${days.map((day,index)=>{const [px,py]=point(day[key],index).split(",");return `<circle cx="${px}" cy="${py}" r="3" fill="currentColor" class="admin-feature-line-${name}"><title>${day.day} ${key} ${day[key]}</title></circle>`;}).join("")}`).join("");el("admin-feature-chart").innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="鍔熻兘浣跨敤瓒嬪娍">${grid}${lines}${labels}</svg>`;el("admin-feature-legend").innerHTML=`<span>鍗曚釜 ${data.totals.single}</span><span>鎵归噺 ${data.totals.batch}</span><span>鏌ユ壘 ${data.totals.discovery}</span>`;}
el("admin-accounts-refresh").addEventListener("click",()=>{state.adminAccountOffset=0;loadAdminAccounts();});el("admin-accounts-prev").addEventListener("click",()=>{state.adminAccountOffset=Math.max(0,state.adminAccountOffset-50);loadAdminAccounts();});el("admin-accounts-next").addEventListener("click",()=>{state.adminAccountOffset+=50;loadAdminAccounts();});
el("admin-account-lookup").addEventListener("click", async()=>{try{await api(`/api/admin/accounts?email=${encodeURIComponent(el("admin-credit-email").value)}`);}catch(error){el("admin-credit-result").textContent=error.message;}});
function renderNotifications() {
  const list = el("notification-list");
  list.replaceChildren();
  if (!state.notifications.length) {
    const empty = document.createElement("p");
    empty.className = "notification-empty";
    empty.textContent = VerigoI18n.text("鏆傛棤閫氱煡");
    list.append(empty);
    return;
  }
  state.notifications.forEach((notification) => {
    const item = document.createElement("article");
    item.className = "notification-item";
    if (notification.target_job_id && notification.target_result_index !== null) {
      item.classList.add("targeted");
      item.title = notification.target_email || "鏌ョ湅瀵瑰簲閭";
      item.addEventListener("click", async () => {
        try {
          state.jobId = notification.target_job_id;
          state.guestToken = null;
          sessionStorage.setItem("verigo_job_id", state.jobId);
          const job = await api(`/api/jobs/${state.jobId}`);
          state.page = Math.floor((notification.target_result_index || 0) / pageSize);
          showJob(job); await loadResults();
          await api(`/api/notifications/${notification.id}/read`, { method: "POST" });
          await api(`/api/jobs/${state.jobId}/results/${notification.target_result_index}/reviewed`, { method: "POST" });
          await loadNotifications();
        } catch (error) {
          errorBox.textContent = error.message;
        }
      });
    }
    const title = document.createElement("strong");
    title.textContent = VerigoI18n.notificationTitle(notification);
    const body = document.createElement("p");
    body.textContent = VerigoI18n.notificationBody(notification);
    const time = document.createElement("time");
    time.textContent = VerigoI18n.formatDate(notification.created_at);
    item.append(title, body, time);
    list.append(item);
  });
}

window.addEventListener("verigo:localechange", renderNotifications);

async function loadNotifications() {
  if (!state.user) return;
  try {
    const payload = await api("/api/notifications");
    state.notifications = payload.items;
    el("notification-count").textContent = payload.unread_count > 99 ? "99+" : String(payload.unread_count);
    el("notification-count").classList.toggle("hidden", !payload.unread_count);
    renderNotifications();
    await loadRecentJobs();
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
        credits: Math.round(Number(el("admin-credit-amount").value) * 200),
        note: el("admin-credit-note").value,
        amount_fen: Math.round(Number(el("admin-credit-amount").value) * 100),
      }),
    });
    result.classList.add("success");
    const amount = Math.abs(adjustment.delta).toLocaleString("zh-CN");
    result.textContent = action === "deduct"
      ? `宸蹭粠 ${adjustment.email} 鎵ｅ噺 ${amount} 棰濆害锛屽綋鍓嶄綑棰?${adjustment.credits.toLocaleString("zh-CN")}銆俙
      : `宸插悜 ${adjustment.email} 鎺堜簣 ${amount} 棰濆害锛屽綋鍓嶄綑棰?${adjustment.credits.toLocaleString("zh-CN")}銆俙;
    el("admin-credit-amount").value = "";
    el("admin-credit-note").value = "";
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
function formatApiKeyTime(value) {
  return value ? VerigoI18n.formatDate(value) : VerigoI18n.text("灏氭湭浣跨敤");
}

function clearCreatedApiKey() {
  el("api-key-token").value = "";
  el("api-key-created").classList.add("hidden");
  el("copy-api-key").textContent = VerigoI18n.text("澶嶅埗");
}

async function loadApiKeys() {
  const list = el("api-keys-list");
  list.textContent = VerigoI18n.text("鍔犺浇涓?..");
  try {
    const keys = await api("/api/auth/api-keys");
    list.replaceChildren();
    if (!keys.length) {
      const empty = document.createElement("p");
      empty.className = "api-keys-empty";
      empty.textContent = VerigoI18n.text("杩樻病鏈?API Key銆?);
      list.append(empty);
      return;
    }
    keys.forEach((key) => {
      const row = document.createElement("div");
      row.className = "api-key-row";
      const info = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = key.name;
      const detail = document.createElement("small");
      detail.textContent = `${key.prefix}... 路 ${formatApiKeyTime(key.last_used_at)}`;
      info.append(name, detail);
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "account-delete";
      revoke.textContent = VerigoI18n.text("鎾ら攢");
      revoke.addEventListener("click", async () => {
        if (!window.confirm(`鎾ら攢 API Key 鈥?{key.name}鈥濓紵姝ゆ搷浣滀笉鑳芥仮澶嶃€俙)) return;
        revoke.disabled = true;
        try {
          await api(`/api/auth/api-keys/${key.id}`, { method: "DELETE" });
          await loadApiKeys();
        } catch (error) {
          revoke.disabled = false;
          el("api-key-create-error").textContent = error.message;
        }
      });
      row.append(info, revoke);
      list.append(row);
    });
  } catch (error) {
    list.textContent = VerigoI18n.locale === "en" ? `Unable to load API keys: ${error.message}` : `鏃犳硶鍔犺浇 API Key锛?{error.message}`;
  }
}

function openApiKeysDialog() {
  el("account-menu").classList.add("hidden");
  el("api-key-create-form").reset();
  el("api-key-create-error").textContent = "";
  clearCreatedApiKey();
  el("api-keys-dialog").showModal();
  loadApiKeys();
}

el("api-nav").addEventListener("click", () => {
  if (!state.user) {
    el("auth-dialog").showModal();
    setAuthMode("login");
    el("auth-error").textContent = VerigoI18n.text("璇峰厛鐧诲綍鍚庣鐞?API Key");
    return;
  }
  openApiKeysDialog();
});
el("close-api-keys").addEventListener("click", () => el("api-keys-dialog").close());
el("api-keys-dialog").addEventListener("close", clearCreatedApiKey);
el("api-keys-refresh").addEventListener("click", loadApiKeys);
el("api-key-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = el("api-key-create-submit");
  submit.disabled = true;
  el("api-key-create-error").textContent = "";
  try {
    const key = await api("/api/auth/api-keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: el("api-key-name").value }),
    });
    el("api-key-token").value = key.token;
    el("api-key-created").classList.remove("hidden");
    el("api-key-create-form").reset();
    await loadApiKeys();
  } catch (error) {
    el("api-key-create-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
el("copy-api-key").addEventListener("click", async () => {
  const token = el("api-key-token").value;
  if (!token) return;
  try {
    await navigator.clipboard.writeText(token);
    el("copy-api-key").textContent = VerigoI18n.text("宸插鍒?);
  } catch (_) {
    el("api-key-token").select();
    document.execCommand("copy");
    el("copy-api-key").textContent = VerigoI18n.text("宸插鍒?);
  }
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
  el("auth-title").textContent = mode === "login" ? "鐧诲綍" : "鍒涘缓璐︽埛";
  el("auth-submit").textContent = mode === "login" ? "鐧诲綍" : "娉ㄥ唽";
  el("auth-account-label").textContent = mode === "login" ? "閭鎴栨棫鐢ㄦ埛鍚? : "閭";
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
    // Keep registration non-blocking; activation remains available from the
    // prominent trial-credit action and can be completed when the user is ready.
    if (window.location.pathname === "/dashboard" && state.user.is_admin) switchView("dashboard");
    if (window.location.pathname === "/admin/credits" && state.user.is_admin) switchView("admin-credits");
    if (state.pendingView && state.user) {
      const nextView = state.pendingView;
      state.pendingView = null;
      switchView(nextView);
    }
    if (state.pendingSaveResult && state.user) {
      const pending = state.pendingSaveResult;
      state.pendingSaveResult = null;
      state.jobId = pending.jobId; state.guestToken = pending.guestToken || state.guestToken;
      state.activeResultItem = { email: pending.email || "", original_index: pending.resultIndex };
      await openSaveResultDialog(state.activeResultItem);
    }
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
el("workspace-history-link")?.addEventListener("click", () => switchView("history"));
el("workspace-lists-link")?.addEventListener("click", () => switchView("lists"));
el("workspace-api-button")?.addEventListener("click", () => el("api-nav").click());
el("lists-nav")?.addEventListener("click", () => switchView("lists"));
document.querySelectorAll("#workspace-home [data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));

(async function init() {
  setAuthMode(state.authMode);
  updateCount();
  await loadAccount();
  await loadPublicConfig();
  if (["/workspace", "/history", "/lists", "/dashboard", "/admin/credits", "/wallet"].includes(window.location.pathname) || window.location.pathname.startsWith("/lists/")) {
    if (window.location.pathname === "/workspace" && state.user) {
      switchView("workspace");
    } else if ((window.location.pathname === "/lists" || window.location.pathname.startsWith("/lists/")) && state.user) {
      switchView("lists");
      if (window.location.pathname.startsWith("/lists/") && window.location.pathname.split("/")[2]) openListDetail(window.location.pathname.split("/")[2]);
    } else if ((window.location.pathname === "/lists" || window.location.pathname.startsWith("/lists/")) && !state.user) {
      window.history.replaceState({}, "", "/"); state.pendingView = "lists"; el("auth-dialog").showModal(); setAuthMode("login"); el("auth-error").textContent = VerigoI18n.text("璇峰厛鐧诲綍鍚庢煡鐪嬭处鎴锋暟鎹?); return;
    } else if (window.location.pathname === "/workspace" && !state.user) {
      window.history.replaceState({}, "", "/");
      switchView("single");
      state.pendingView = "workspace";
      el("auth-dialog").showModal();
      setAuthMode("login");
      el("auth-error").textContent = "Please sign in to open your workspace";
      return;
    } else if (window.location.pathname === "/history" && state.user) {
      switchView("history");
    } else if (window.location.pathname === "/history" && !state.user) {
      window.history.replaceState({}, "", "/");
      state.pendingView = "history";
      el("auth-dialog").showModal();
      setAuthMode("login");
      el("auth-error").textContent = VerigoI18n.text("璇峰厛鐧诲綍鍚庢煡鐪嬭处鎴锋暟鎹?);
      return;
    } else if (window.location.pathname === "/wallet" && state.user) {
      switchView("wallet");
    } else if (state.user?.is_admin) {
      switchView(window.location.pathname === "/admin/credits" ? "admin-credits" : "dashboard");
    } else if (state.user) {
      window.location.replace("/");
      return;
    } else {
      el("auth-dialog").showModal();
      setAuthMode("login");
      el("auth-error").textContent = "璇风櫥褰曟湁杩愯惀鐩戞帶鏉冮檺鐨勮处鎴?;
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
