const initialPath = window.location.pathname;
const initialView = initialPath === "/workspace" || initialPath === "/lists" ? "workspace"
  : initialPath === "/dashboard" || initialPath === "/admin" ? "dashboard"
  : initialPath === "/admin/credits" ? "admin-credits"
    : initialPath === "/wallet" || initialPath === "/app/billing" ? "wallet"
      : initialPath === "/history" || initialPath === "/app/history" ? "history"
        : initialPath === "/app/finder" ? "discovery" : "single";
const state = {
  view: initialView,
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
  notificationLoading: false,
  notificationUnread: 0,
  notificationTotal: 0,
  notificationOffset: 0,
  notificationLimit: 30,
  notificationFilter: ["all", "unread", "verification", "account"].includes(localStorage.getItem("verigo_notification_filter"))
    ? localStorage.getItem("verigo_notification_filter") : "all",
  notificationPreferences: {
    compact: localStorage.getItem("verigo_notification_compact") === "1",
    autoRefresh: localStorage.getItem("verigo_notification_auto_refresh") !== "0",
  },
  recentJobs: { offset: 0, limit: 8, total: 0 },
  adminAccountOffset: 0,
  retryCountdownTimer: null,
  onboardingTimer: null,
  workspace: { loaded: false },
  pendingView: null,
  pendingRedemption: false,
  activeResultItem: null,
  history: { offset: 0, limit: 10, total: 0 },
  historyTimer: null,
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
      const gap = document.createElement("span"); gap.className = "page-gap"; gap.textContent = "…"; container.append(gap);
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
const statusLabels = { queued: "排队中", running: "验证中", completed: "已完成", failed: "失败", stopped: "已停止" };
const modeLabels = {
  1: ["验证任务", "mode-standard"],
  2: ["验证任务", "mode-standard"],
  4: ["验证任务", "mode-standard"],
  8: ["验证任务", "mode-standard"],
};

const emailPattern = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$/;

function inputTokens(text) {
  return String(text || "").split(/[\s,;，；]+/).map((value) => value.trim()).filter(Boolean);
}

function normalizeEmails(values) {
  const seen = new Set();
  return values.filter((value) => {
    const email = String(value).trim();
    const key = email.toLowerCase();
    if (!emailPattern.test(email) || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).map((value) => String(value).trim());
}

function splitEmails(text) {
  return normalizeEmails(inputTokens(text));
}

function updateBatchInputSummary() {
  const summary = state.mode === "file" ? el("file-input-summary") : el("batch-input-summary");
  const cleanButton = el("clean-email-input");
  if (!summary) return;
  const source = state.mode === "file" ? state.fileEmails : inputTokens(batchInput?.value);
  const emails = normalizeEmails(source);
  const invalid = source.filter((value) => !emailPattern.test(String(value).trim())).length;
  const duplicates = Math.max(0, source.length - invalid - emails.length);
  const blankLines = state.mode === "paste"
    ? String(batchInput?.value || "").split(/\r?\n/).filter((line) => !line.trim()).length : 0;
  if (!source.length && !blankLines) {
    summary.textContent = state.mode === "file" ? "导入后将自动去重，空白和无效地址不会计费。" : "将自动忽略空白行、无效地址和重复邮箱。";
  } else {
    const removed = [];
    if (duplicates) removed.push(`重复 ${duplicates} 条`);
    if (blankLines) removed.push(`空白 ${blankLines} 行`);
    if (invalid) removed.push(`无效 ${invalid} 条`);
    summary.textContent = `实际验证 ${emails.length.toLocaleString()} 条${removed.length ? `，已排除 ${removed.join("、")}` : "，不会重复计费"}。`;
  }
  if (cleanButton) cleanButton.disabled = state.mode !== "paste" || (!duplicates && !blankLines && !invalid);
}

function currentEmails() {
  if (state.view === "single") return splitEmails(singleInput?.value || "");
  return state.mode === "file" ? state.fileEmails : splitEmails(batchInput?.value || "");
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
  if (!notice) return;
  const hasQq = emails.some(isQqEmail);
  notice.classList.toggle("hidden", !hasQq);
  notice.textContent = hasQq
    ? VerigoI18n.text("检测到 QQ 邮箱：将采用专属低并发与自动退避策略，验证速度会较慢，请耐心等待。")
    : "";
}

function updateCount() {
  const emails = currentEmails();
  const total = emails.length;
  updateProviderNotice(emails);
  updateBatchInputSummary();
  if (!count || !startButton) return;
  count.textContent = total.toLocaleString();
  if (state.view === "single") {
    startButton.textContent = VerigoI18n.text("免费验证");
  } else if (total > 0) {
    startButton.textContent = VerigoI18n.text(`开始验证 · ${total.toLocaleString()} 额度`);
  } else {
    startButton.textContent = VerigoI18n.text("开始验证");
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
    throw new Error(VerigoI18n.errorMessage(message || `请求失败 (${response.status})`));
  }
  return body;
}

function switchView(view) {
  const adminView = view === "dashboard" || view === "admin-credits" || view === "system-health";
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
  const systemHealth = view === "system-health";
  const wallet = view === "wallet";
  const workspace = view === "workspace";
  const history = view === "history";
  if (wallet && !state.user) { state.pendingView = view; el("auth-dialog").showModal(); setAuthMode("login"); el("auth-error").textContent = VerigoI18n.text("请先登录后查看账户数据"); return; }
  if (workspace && !state.user) { state.pendingView = "workspace"; el("auth-dialog").showModal(); setAuthMode("login"); el("auth-error").textContent = "Please sign in to open your workspace"; return; }
  if (history && !state.user) {
    el("auth-dialog").showModal();
    setAuthMode("login");
    el("auth-error").textContent = "请先登录后查看历史记录";
    return;
  }
  if (discovery && !state.user) {
    el("auth-dialog").showModal();
    setAuthMode("login");
    el("auth-error").textContent = "请先登录后使用企业邮箱查找";
    return;
  }
  state.view = view;
  const marketing = view === "single" || view === "batch";
  document.querySelectorAll(".public-marketing").forEach((section) => section.classList.toggle("workspace-mode-hidden", !marketing));
  const setHidden = (id, hidden) => el(id)?.classList.toggle("hidden", hidden);
  setHidden("verify-workspace", discovery || dashboard || adminCredits || systemHealth || wallet || workspace || history);
  setHidden("workspace-home", !workspace);
  setHidden("discovery-workspace", !discovery);
  setHidden("dashboard-workspace", !dashboard);
  setHidden("admin-credits-workspace", !adminCredits);
  setHidden("system-health-workspace", !systemHealth);
  setHidden("wallet-workspace", !wallet);
  setHidden("history-workspace", !history);
  setHidden("single-panel", view !== "single");
  setHidden("batch-panel", view !== "batch");
  if (!discovery && !dashboard && !adminCredits && !systemHealth && !wallet && !workspace && !history) {
    el("verify-eyebrow").textContent = VerigoI18n.text(view === "single" ? "免费单个验证" : "收费批量验证");
    el("verify-heading").textContent = VerigoI18n.text(view === "single" ? "验证单个收件地址" : "批量验证收件地址");
  }
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  if (discovery) {
    document.title = "邮箱查找 | Verigo";
    if (window.location.pathname !== "/app/finder") window.history.pushState({}, "", "/app/finder");
  } else if (dashboard) {
    document.title = `${VerigoI18n.text("运营监控")} | Verigo`;
    if (window.location.pathname !== "/dashboard") window.history.pushState({}, "", "/dashboard");
    loadDashboardMetrics();
    clearInterval(state.metricsTimer);
    state.metricsTimer = window.setInterval(loadDashboardMetrics, 30000);
  } else if (adminCredits) {
    document.title = `${VerigoI18n.text("额度管理")} | Verigo`;
    if (window.location.pathname !== "/admin/credits") window.history.pushState({}, "", "/admin/credits");
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
    loadAdminAccounts();
    loadAdminFeatureUsage();
  } else if (systemHealth) {
    document.title = "系统监控 | Verigo";
    if (window.location.pathname !== "/admin/system") window.history.pushState({}, "", "/admin/system");
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
    loadSystemHealth();
  } else if (wallet) {
    document.title = `${VerigoI18n.text("资金与使用")} | Verigo`;
    if (window.location.pathname !== "/wallet") window.history.pushState({}, "", "/wallet");
    loadWallet();
  } else if (workspace) {
    document.title = "Workspace | Verigo";
    if (window.location.pathname !== "/workspace") window.history.pushState({}, "", "/workspace");
    loadWorkspaceHome();
  } else if (history) {
    document.title = "历史记录 | Verigo";
    if (!state.user) { el("auth-dialog").showModal(); return; }
    if (window.location.pathname !== "/history") window.history.pushState({}, "", "/history");
    loadHistoryPage();
  } else {
    document.title = `${VerigoI18n.text(view === "batch" ? "批量验证" : "邮箱验证")} | Verigo`;
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
    if (window.location.pathname !== "/verify") window.history.replaceState({}, "", "/verify");
  }
  updateCount();
}

window.addEventListener("popstate", () => {
  const pathView = { "/workspace": "workspace", "/lists": "workspace", "/dashboard": "dashboard", "/admin/credits": "admin-credits", "/wallet": "wallet", "/history": "history", "/app/finder": "discovery", "/app/history": "history", "/app/billing": "wallet" }[window.location.pathname] || "single";
  switchView(pathView);
});

function formatMoney(fen) {
  return `¥${(Number(fen || 0) / 100).toFixed(2)}`;
}

function setMetric(id, value) {
  el(id).textContent = Number(value || 0).toLocaleString("zh-CN");
}

function qualityCount(value) {
  return Math.max(0, Number(value) || 0);
}

function renderQualityOverview(quality) {
  const section = el("quality-dashboard");
  if (!section) return;

  const data = quality && typeof quality === "object" ? quality : {};
  const total = qualityCount(data.total);
  const deliverable = Math.min(total, qualityCount(data.deliverable));
  const unknown = Math.min(total, qualityCount(data.unknown));
  const reviewed = qualityCount(data.reviewed);
  const attentionList = el("quality-attention-list");

  setMetric("quality-verification-total", total);
  el("quality-deliverable-rate").textContent = total ? `${(deliverable / total * 100).toFixed(1)}%` : "—";
  el("quality-unknown-rate").textContent = total ? `${(unknown / total * 100).toFixed(1)}%` : "—";
  setMetric("quality-reviewed-count", reviewed);
  el("quality-summary-copy").textContent = total ? `基于 ${total.toLocaleString("zh-CN")} 条近期记录` : "暂无足够数据";

  attentionList.replaceChildren();
  if (!total) {
    const empty = document.createElement("li");
    empty.className = "quality-attention-empty";
    empty.textContent = "暂无足够数据";
    attentionList.append(empty);
    return;
  }

  const flags = data.risk_flags && typeof data.risk_flags === "object" ? data.risk_flags : {};
  const entries = [
    ["一次性邮箱", flags.disposable],
    ["收件箱已满", flags.mailbox_full],
    ["角色邮箱", flags.role_address],
    ["不应回复", flags.do_not_reply],
  ].map(([label, value]) => [label, qualityCount(value)]).filter(([, value]) => value > 0);

  if (!entries.length) {
    const empty = document.createElement("li");
    empty.className = "quality-attention-empty";
    empty.textContent = "当前没有需要关注的记录";
    attentionList.append(empty);
    return;
  }

  entries.forEach(([label, value]) => {
    const item = document.createElement("li");
    const name = document.createElement("span");
    const count = document.createElement("strong");
    name.textContent = label;
    count.textContent = value.toLocaleString("zh-CN");
    item.append(name, count);
    attentionList.append(item);
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
}

function cloudShellStatusLabel(status) {
  return ({ active: "活跃", idle: "待机", cooldown: "冷却中", disabled: "已禁用" }[status] || "待确认");
}

function cloudShellHealthLabel(health) {
  return ({ healthy: "健康", stale: "心跳滞后", offline: "离线", unknown: "未连接" }[health] || "未确认");
}

function renderCloudshellAccounts(payload) {
  const summary = payload.summary || {};
  [
    ["cloudshell-total-accounts", summary.total_accounts],
    ["cloudshell-active-accounts", summary.active_accounts],
    ["cloudshell-cooldown-accounts", summary.cooldown_accounts],
    ["cloudshell-today-units", summary.today_units],
    ["cloudshell-queue-depth", summary.queue_depth],
  ].forEach(([id, value]) => setMetric(id, value));
  const cards = el("cloudshell-account-cards");
  const items = Array.isArray(payload.items) ? payload.items : [];
  if (!items.length) {
    cards.innerHTML = '<div class="cloudshell-crm-state">暂无已配置的 CloudShell 账号</div>';
    return;
  }
  cards.innerHTML = items.map((account) => {
    const status = escapeHtml(account.status || "unknown");
    const health = escapeHtml(account.health || "unknown");
    const claimedUnits = Number(account.claimed_units || 0).toLocaleString("zh-CN");
    const softQuota = Number(account.soft_quota_units || 0);
    const quotaText = softQuota > 0 ? `${claimedUnits} / ${softQuota.toLocaleString("zh-CN")}` : `${claimedUnits} / 未设置上限`;
    const lastSeen = account.last_seen_at ? new Date(account.last_seen_at).toLocaleString("zh-CN") : "暂无心跳";
    const lastClaimed = account.last_claimed_at ? new Date(account.last_claimed_at).toLocaleString("zh-CN") : "尚未领取";
    return `<article class="cloudshell-account-card cloudshell-status-${status}" data-worker-id="${escapeHtml(account.worker_id)}">
      <div class="cloudshell-account-card-top"><div class="cloudshell-account-avatar" aria-hidden="true">CS</div><div class="cloudshell-account-name"><strong>${escapeHtml(account.account_id)}</strong><span>${escapeHtml(account.worker_id)}</span></div><span class="cloudshell-status-badge cloudshell-status-${status}">${cloudShellStatusLabel(account.status)}</span></div>
      <div class="cloudshell-account-health"><span class="cloudshell-health-dot cloudshell-health-${health}"></span>${cloudShellHealthLabel(account.health)}<span class="cloudshell-health-time">${escapeHtml(lastSeen)}</span></div>
      <dl class="cloudshell-account-stats"><div><dt>今日处理邮箱</dt><dd>${claimedUnits}</dd></div><div><dt>今日任务</dt><dd>${Number(account.claimed_tasks || 0).toLocaleString("zh-CN")}</dd></div><div><dt>失败次数</dt><dd>${Number(account.failure_count || 0).toLocaleString("zh-CN")}</dd></div><div><dt>软配额</dt><dd>${escapeHtml(quotaText)}</dd></div></dl>
      <div class="cloudshell-account-footer"><span>最近领取</span><strong>${escapeHtml(lastClaimed)}</strong></div>
    </article>`;
  }).join("");
}

async function loadCloudshellAccounts() {
  if (!state.user?.is_admin || state.view !== "dashboard") return;
  try {
    const data = await api("/api/admin/cloudshell/accounts");
    renderCloudshellAccounts(data);
    const updatedAt = data.summary?.updated_at;
    el("cloudshell-crm-updated").textContent = updatedAt
      ? `最近更新：${new Date(updatedAt).toLocaleString("zh-CN")}`
      : "账号池状态已更新";
  } catch (error) {
    el("cloudshell-crm-updated").textContent = "账号状态加载失败，请稍后刷新";
    el("cloudshell-account-cards").innerHTML = `<div class="cloudshell-crm-state cloudshell-crm-state-error">${escapeHtml(error.message || "无法读取账号状态")}</div>`;
  }
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

function renderProviderQuality(quality) {
  const body = el("provider-quality-body");
  if (!body) return;
  const labels = {
    gmail: "Gmail",
    microsoft: "\u5fae\u8f6f\u90ae\u7bb1",
    qq: "QQ \u90ae\u7bb1",
    other: "\u5176\u4ed6\u90ae\u7bb1",
  };
  const rows = Array.isArray(quality?.providers) ? quality.providers : [];
  const rate = (value) => value == null ? "\u2014" : Number(value).toFixed(1);
  const getQualityColor = (value) => {
    if (value == null) return "#e8eaed";
    const num = Number(value);
    if (num >= 90) return "#34a853";
    if (num >= 75) return "#fbbc04";
    return "#ea4335";
  };
  const getQualityLevel = (value) => {
    if (value == null) return "unknown";
    const num = Number(value);
    if (num >= 90) return "excellent";
    if (num >= 75) return "good";
    return "poor";
  };
  body.innerHTML = rows.map((item) => {
    const processed = Math.max(0, Number(item.processed) || 0);
    const deliverableRate = rate(item.deliverable_rate);
    const unconfirmedRate = rate(item.unconfirmed_rate);
    const reviewRate = rate(item.review_completion_rate);
    const deliverableNum = Number(item.deliverable_rate) || 0;
    const unconfirmedNum = Number(item.unconfirmed_rate) || 0;
    const reviewNum = Number(item.review_completion_rate) || 0;
    const latencySample = Math.max(0, Number(item.latency_sample) || 0);
    const initialDuration = latencySample ? `${formatDuration(item.p50_seconds)} / ${formatDuration(item.p95_seconds)}` : "\u2014";
    return `<tr>
      <th scope="row"><strong>${labels[item.provider] || labels.other}</strong></th>
      <td>${processed.toLocaleString("zh-CN")}</td>
      <td>
        <div class="quality-progress-wrapper">
          <div class="quality-progress-bar">
            <div class="quality-progress-fill" style="width:${deliverableNum}%;background:${getQualityColor(deliverableNum)}"></div>
          </div>
          <span class="quality-value">${deliverableRate}%</span>
        </div>
      </td>
      <td>
        <div class="quality-progress-wrapper">
          <div class="quality-progress-bar">
            <div class="quality-progress-fill" style="width:${unconfirmedNum}%;background:${getQualityColor(unconfirmedNum)}"></div>
          </div>
          <span class="quality-value">${unconfirmedRate}%</span>
        </div>
      </td>
      <td>
        <div class="quality-progress-wrapper">
          <div class="quality-progress-bar">
            <div class="quality-progress-fill" style="width:${reviewNum}%;background:${getQualityColor(reviewNum)}"></div>
          </div>
          <span class="quality-value">${reviewRate}%</span>
        </div>
      </td>
      <td><span class="provider-latency"><span>\u9996\u6b21 ${initialDuration}</span></span></td>
    </tr>`;
  }).join("") || '<tr><td colspan="6" class="provider-quality-empty">\u6700\u8fd1 24 \u5c0f\u65f6\u6682\u65e0\u5df2\u5b8c\u6210\u7ed3\u679c</td></tr>';
}

async function loadDashboardMetrics() {
  if (!state.user?.is_admin || state.view !== "dashboard") return;
  try {
    const data = await api("/api/admin/metrics");
    const today = data.today;
    renderQualityOverview(data.provider_quality);
    renderProviderQuality(data.provider_quality);
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
    el("metric-job-queue-duration").textContent = formatDuration(today.average_queue_seconds);
    el("metric-job-retry-duration").textContent = formatDuration(today.average_retry_wait_seconds);
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
    loadCloudshellAccounts();
  } catch (error) {
    el("dashboard-updated").textContent = `数据加载失败：${error.message}`;
  }
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});
document.querySelectorAll("#account-menu [data-view]").forEach((button) => {
  button.addEventListener("click", () => el("account-menu").classList.add("hidden"));
});

document.querySelectorAll(".verification-type-tabs [data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".verification-type-tabs [data-view]").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", active ? "true" : "false");
    });
  });
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

batchInput?.addEventListener("input", updateCount);
singleInput?.addEventListener("input", updateCount);
el("clean-email-input")?.addEventListener("click", () => {
  batchInput.value = splitEmails(batchInput.value).join("\n");
  batchInput.focus();
  updateCount();
});
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

el("file-input")?.addEventListener("change", (event) => importFile(event.target.files[0]));
const dropzone = el("file-dropzone");
["dragenter", "dragover"].forEach((name) => dropzone?.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => dropzone?.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
}));
dropzone?.addEventListener("drop", (event) => importFile(event.dataTransfer.files[0]));

startButton?.addEventListener("click", async () => {
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
    // Keep the existing API field while choosing capacity internally.
    const workerCount = isFreeSingle ? 1 : 4;
    const job = await api(isFreeSingle ? "/api/verify/single" : "/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(isFreeSingle
        ? { email: emails[0], list_name: el("single-list-name-input")?.value.trim() || null }
        : { emails, worker_count: workerCount, list_name: (el("list-name-input")?.value.trim() || (state.mode === "file" ? el("file-input").files[0]?.name || null : null)) }),
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
    || (job.status === "queued" && job.queue_position ? `排队中，前方还有 ${job.queue_position - 1} 个任务` : `${job.completed} / ${job.total} 已处理`);
  renderJobProgress(job, progressCopy);
  if (job.summary) renderSummary(job.summary);
  el("download-button").disabled = !job.download_url;
}

function renderJobProgress(job, progressCopy) {
  clearInterval(state.retryCountdownTimer);
  const suffix = job.qq_slow ? "；QQ 邮箱采用低并发和自动退避策略，请耐心等待。" : "";
  const retryAt = job.retry_at ? new Date(job.retry_at) : null;
  const render = () => {
    if (!retryAt || Number.isNaN(retryAt.getTime())) {
      el("progress-copy").textContent = VerigoI18n.text(`${progressCopy}${suffix}`);
      return;
    }
    const seconds = Math.ceil((retryAt.getTime() - Date.now()) / 1000);
    if (seconds <= 0) {
      el("progress-copy").textContent = VerigoI18n.text(`${progressCopy}${suffix}`);
      clearInterval(state.retryCountdownTimer);
      state.retryCountdownTimer = null;
      return;
    }
    const countdown = seconds >= 60
      ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
      : `${seconds} 秒`;
    el("progress-copy").textContent = VerigoI18n.text(`${progressCopy}，${countdown} 后再次复核${suffix}`);
  };
  render();
  if (retryAt && retryAt.getTime() > Date.now()) {
    state.retryCountdownTimer = window.setInterval(() => { render(); renderResults(); }, 1000);
  }
}

function retryReviewStatus(item) {
  if (item.retry_state === "scheduled") {
    const attempt = Number(item.retry_attempt || 1);
    const maximum = Number(item.retry_max_attempts || 3);
    return `已安排自动复核（第 ${attempt}/${maximum} 次）`;
  }
  if (item.retry_state === "failed") return "自动复核未完成；当前结果仍无法确认，建议稍后再次验证。";
  if (item.temporary_retries_exhausted) return "自动复核已结束；当前结果仍无法确认，建议稍后再次验证。";
  if (item.greylist_retry_exhausted) return "自动复核已结束；当前结果仍无法确认，建议稍后再次验证。";
  return null;
}

function formatJobName(timestamp) {
  const label = VerigoI18n.locale === "en" ? "Email verification" : "邮箱验证";
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
      const retryAt = job.retry_at ? new Date(job.retry_at) : null;
      if (job.status === "completed" && retryAt && retryAt.getTime() > Date.now()) {
        schedulePoll(2000);
      } else {
        clearInterval(state.retryCountdownTimer);
        state.retryCountdownTimer = null;
      }
    } else if (job.status !== "failed") {
      schedulePoll();
    }
  } catch (error) {
    // Recent jobs are a background enhancement. A stale session (or a test
    // fixture that only mocks /auth/me) must not overwrite the active job UI.
    if (!/sign in|登录|鐧诲綍/i.test(error.message || "")) errorBox.textContent = error.message;
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
  if (item.progress_state === "pending") return ["等待验证", "result-pending", "pending"];
  if (item.progress_state === "verifying") return ["验证中", "result-running", "verifying"];
  if (item.progress_state === "failed") return ["未完成", "result-failed", "failed"];
  if (item.skipped) return ["已停止", "result-skipped", "skipped"];
  if (item.deliverable === true) return ["可投递", "result-good", "deliverable"];
  if (item.deliverable === false) return ["不可投递", "result-bad", "undeliverable"];
  return ["无法确认", "result-unknown", "unknown"];
}

function consumerResultAction(item) {
  if (item.progress_state === "pending" || item.progress_state === "verifying") return VerigoI18n.text("正在验证中...");
  if (item.progress_state === "failed") return VerigoI18n.text("验证失败，请重新验证");
  if (item.skipped) return VerigoI18n.text("验证已取消");
  if (item.deliverable === true) return VerigoI18n.text("有效邮箱，可直接使用");
  if (item.risk_signals?.mailbox_full?.detected === true) return VerigoI18n.text("收件箱已满，暂时无法接收邮件");
  if (item.failure_reason === "domain_nxdomain") return VerigoI18n.text("域名不存在，请检查拼写");
  if (item.failure_reason === "mx_missing") return VerigoI18n.text("该域名未配置邮件服务");
  if (item.deliverable === false) return VerigoI18n.text("无效邮箱，建议删除");
  return retryReviewStatus(item) || VerigoI18n.text("状态未知，建议稍后重试");
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
    cell.colSpan = 4;
    cell.textContent = state.results.length ? "没有符合条件的结果" : "正在等待首条验证结果";
    row.append(cell);
    body.append(row);
    renderPagination();
    return;
  }
  rows.forEach((item) => {
    const [label, className] = resultMeta(item);
    const row = document.createElement("tr");
    row.className = "result-email-row";
    const values = [
      item.email,
      label,
      consumerResultAction(item),
      "详情",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 0) {
        cell.className = "result-email-cell";
        const emailContent = document.createElement("span");
        emailContent.className = "result-email-content";
        const email = document.createElement("span");
        email.className = "result-email-value";
        email.textContent = item.email;
        emailContent.append(email);
        // Keep the row compact while making the most common action available
        // as soon as the pointer reaches the email.
        if (item.progress_state !== "pending" && item.progress_state !== "verifying") {
          const copy = document.createElement("button");
          copy.type = "button";
          copy.className = "copy-button result-copy-button";
          copy.setAttribute("aria-label", VerigoI18n.text("复制邮箱"));
          copy.title = VerigoI18n.text("复制邮箱");
          copy.innerHTML = '<i class="fa-regular fa-copy" aria-hidden="true"></i>';
          copy.addEventListener("click", async (event) => {
            event.stopPropagation();
            await copyEmailValue(item.email, copy);
            if (item.retry_updated) {
              await api(`/api/jobs/${state.jobId}/results/${item.original_index}/reviewed`, { method: "POST" });
              item.retry_updated = false;
              await loadRecentJobs();
            }
          });
          emailContent.append(copy);
        }
        cell.append(emailContent);
        if (item.retry_updated) {
          const dot = document.createElement("i"); dot.className = "result-email-update";
          dot.title = VerigoI18n.text("该邮箱的复核结果已更新"); cell.append(dot);
        }
      } else if (index === 1) {
        const pill = document.createElement("span");
        pill.className = `result-pill ${className}`;
        pill.textContent = label;
        cell.append(pill);
      } else if (index === 3) {
        const action = document.createElement("button");
        action.type = "button";
        action.className = "result-detail-action";
        action.title = "查看详情";
        action.setAttribute("aria-label", `查看 ${item.email} 的详情`);
        action.innerHTML = '<i class="fa-solid fa-circle-info" aria-hidden="true"></i>';
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

async function copyEmailValue(email, button) {
  if (!email) return;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(email);
    else {
      const area = document.createElement("textarea"); area.value = email;
      area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0";
      document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
    }
    const icon = button.querySelector("i");
    if (icon) { icon.className = "fa-solid fa-check"; }
    button.classList.add("copied");
    button.title = VerigoI18n.text("已复制");
    window.setTimeout(() => {
      if (!button.isConnected) return;
      if (icon) icon.className = "fa-regular fa-copy";
      button.classList.remove("copied");
      button.title = VerigoI18n.text("复制邮箱");
    }, 1200);
  } catch (error) {
    errorBox.textContent = error.message || VerigoI18n.text("复制失败，请重试");
  }
}

function renderPagination() {
  const available = state.resultsAvailable;
  const start = available ? state.page * pageSize + 1 : 0;
  const end = Math.min((state.page + 1) * pageSize, available);
  el("results-page-info").textContent = available ? `已显示 ${start}-${end}，共 ${available} 个邮箱` : "等待验证结果";
  el("previous-page").disabled = state.page === 0;
  el("next-page").disabled = (state.page + 1) * pageSize >= available;
  renderPageNumbers(el("results-pages"), state.page, Math.max(1, Math.ceil(available / pageSize)), async (page) => {
    state.page = page; await loadResults();
  });
}

const riskSignalPresentation = [
  { key: "disposable_provider", label: "一次性邮箱", level: "block", detected: "一次性邮箱服务", detail: "该地址可能很快失效。不要将其用于长期联系。" },
  { key: "free_provider", label: "免费邮箱", level: "caution", detected: "公共免费邮箱", detail: "该地址不属于企业自有域名。联系前应结合联系人身份确认。" },
  { key: "role_address", label: "角色邮箱", level: "caution", detected: "团队共享地址", detail: "该地址由多人共同接收，适合一般咨询，不适合个人化联系。" },
  { key: "tagged_address", label: "邮箱标签", level: "caution", detected: "包含地址标签", detail: "邮件通常会投递到主地址。按原地址发送即可。" },
  { key: "mailbox_full", label: "邮箱容量", level: "block", detected: "收件箱已满", detail: "该地址当前无法接收新邮件。等待对方清理容量后再联系。" },
  { key: "do_not_reply", label: "回复意图", level: "block", detected: "不应回复", detail: "这通常是系统发信地址，回复可能不会被处理。请更换可联系地址。" },
  { key: "irregular_pattern", label: "地址模式", level: "block", detected: "异常字符模式", detail: "该地址的字符模式异常，可能影响投递。建议人工确认后再发送。" },
  { key: "unicode_or_suspicious_characters", label: "字符范围", level: "block", detected: "特殊字符", detail: "该地址含有部分系统不支持的字符，可能影响投递。建议人工确认后再发送。" },
  { key: "secure_email_gateway", label: "安全网关", level: "caution", detected: "额外投递规则", detail: "对方可能有额外的投递规则。首次联系应监控退信并逐步发送。" },
];

function detailSection(title, note = "") {
  const heading = document.createElement("div");
  heading.className = "detail-section-heading";
  const label = document.createElement("h3");
  label.textContent = title;
  heading.append(label);
  if (note) {
    const copy = document.createElement("p");
    copy.textContent = note;
    heading.append(copy);
  }
  return heading;
}

function riskSignalStatus(presentation, signal) {
  if (signal.detected === true) return { value: presentation.detected, className: `risk-${presentation.level}` };
  // An unconfirmed disposable-domain result must not be presented as clear.
  if (presentation.key === "disposable_provider") {
    return { value: "暂无法确认", className: "risk-unknown" };
  }
  if (signal.detected === false) return { value: "未识别", className: "risk-clear" };
  return { value: "暂无法确认", className: "risk-unknown" };
}

function riskSignalDetail(presentation, signal) {
  if (signal.detected !== true) {
    if (presentation.key === "disposable_provider") {
      return "当前无法确认该地址是否来自一次性邮箱服务。建议不要将其作为长期联系人地址。";
    }
    return signal.detected === false ? "本次未发现该特征，无需额外处理。" : "当前无法确认该特征，不影响已显示的验证结果。";
  }
  return presentation.detail;
}

function openResultDetails(item) {
  state.activeResultItem = item;
  const drawer = el("result-detail-drawer");
  el("result-detail-title").textContent = item.email || "邮箱详情";
  const [statusLabel, statusClass] = resultMeta(item);
  const status = el("result-detail-status");
  status.textContent = statusLabel;
  status.className = `result-pill ${statusClass}`;
  const checks = item.checks && typeof item.checks === "object" ? item.checks : {};
  const checkLabel = (value) => value === true ? "通过" : value === false ? "未通过" : "待确认";
  const checkClass = (value) => value === true ? "check-good" : value === false ? "check-bad" : "check-pending";
  const content = el("result-detail-content");
  content.replaceChildren();
  content.append(detailSection("验证结论", "先确认当前结论和建议，再查看检查项与技术信息。"));
  const fields = [
    ["邮箱状态", statusLabel],
    ["下一步", consumerResultAction(item)],
  ];
  const reviewStatus = retryReviewStatus(item);
  if (reviewStatus) fields.push(["复核状态", reviewStatus]);
  fields.forEach(([label, value]) => {
    const row = document.createElement("div"); row.className = "detail-field detail-conclusion-field";
    const key = document.createElement("span"); key.textContent = label;
    const val = document.createElement("strong"); val.textContent = value;
    row.append(key, val); content.append(row);
  });

  content.append(detailSection("可投递性检查"));
  const checkGrid = document.createElement("div");
  checkGrid.className = "detail-check-grid";
  [["语法格式", checks.format], ["邮箱域名", checks.domain], ["MX 记录", checks.mx], ["SMTP 连接", checks.smtp]].forEach(([label, value]) => {
    const item = document.createElement("div"); item.className = "detail-check";
    const key = document.createElement("span"); key.textContent = label;
    const val = document.createElement("strong"); val.className = checkClass(value); val.textContent = checkLabel(value);
    item.append(key, val); checkGrid.append(item);
  });
  content.append(checkGrid);
  const riskSignals = item.risk_signals && typeof item.risk_signals === "object" ? item.risk_signals : {};
  const detectedRiskCount = riskSignalPresentation.filter(({ key }) => riskSignals[key]?.detected === true).length;
  const hasRiskSignals = riskSignalPresentation.some(({ key }) => riskSignals[key] && typeof riskSignals[key] === "object");
  content.append(detailSection(
    "地址特征与投递风险",
    !hasRiskSignals ? "该历史结果尚未提供风险信号。"
      : detectedRiskCount ? `已识别 ${detectedRiskCount} 项需要关注的特征。` : "本次未识别到需要特别关注的地址特征。",
  ));
  const riskGrid = document.createElement("div"); riskGrid.className = "detail-check-grid";
  riskSignalPresentation.forEach((presentation) => {
    const signal = riskSignals[presentation.key] && typeof riskSignals[presentation.key] === "object" ? riskSignals[presentation.key] : {};
    const status = riskSignalStatus(presentation, signal);
    const item = document.createElement("div"); item.className = "detail-check";
    const key = document.createElement("span"); key.textContent = presentation.label;
    const val = document.createElement("strong");
    val.className = status.className;
    val.textContent = status.value;
    const detail = document.createElement("small");
    detail.textContent = riskSignalDetail(presentation, signal);
    item.append(key, val, detail); riskGrid.append(item);
  });
  content.append(riskGrid);
  // Add technical details section if available
  const hasTechnicalDetails = item.smtp_result || item.smtp_raw_result || item.message || item.failure_reason || item.verification_method;
  if (hasTechnicalDetails && ["done", "completed"].includes(item.progress_state)) {
    const technicalDetails = document.createElement("details");
    technicalDetails.className = "technical-details";
    const technicalSummary = document.createElement("summary");
    const technicalSummaryTitle = document.createElement("strong");
    technicalSummaryTitle.textContent = "技术详情";
    const technicalSummaryNote = document.createElement("span");
    technicalSummaryNote.textContent = "仅供参考，帮助诊断问题";
    technicalSummary.append(technicalSummaryTitle, technicalSummaryNote);
    technicalDetails.append(technicalSummary);
    const technicalBody = document.createElement("div");
    technicalBody.className = "technical-details-body";

    const technicalFields = [];

    // Verification method
    if (item.verification_method) {
      technicalFields.push(["验证方式", item.verification_method]);
    }

    // Failure reason (human-readable)
    if (item.failure_reason) {
      const reasonMap = {
        "domain_nxdomain": "域名不存在 (NXDOMAIN)",
        "mx_missing": "邮件服务器未配置",
        "smtp_connection_failed": "SMTP连接失败",
        "smtp_reject": "邮件服务器拒绝",
        "mailbox_not_found": "邮箱不存在",
        "mailbox_full": "邮箱已满",
        "timeout": "验证超时",
      };
      const reasonText = reasonMap[item.failure_reason] || item.failure_reason;
      technicalFields.push(["失败原因", reasonText]);
    }

    // SMTP response (most detailed)
    const smtpResponse = item.smtp_result || item.smtp_raw_result || item.message;
    if (smtpResponse && smtpResponse !== "正在验证" && smtpResponse !== item.verification_method) {
      technicalFields.push(["服务器响应", smtpResponse]);
    }

    technicalFields.forEach(([label, value]) => {
      const row = document.createElement("div"); row.className = "detail-field technical-detail";
      const key = document.createElement("span"); key.textContent = label;
      const val = document.createElement("span");
      val.className = "technical-value";
      val.textContent = value;
      row.append(key, val); technicalBody.append(row);
    });
    technicalDetails.append(technicalBody);
    content.append(technicalDetails);
  }

  drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false");
}

function closeResultDetails() {
  const drawer = el("result-detail-drawer");
  drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true");
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
  if (!state.jobId || state.copyInFlight) return;
  state.copyInFlight = true;
  try {
    const items = [];
    const limit = 500;
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
    errorBox.textContent = `已复制 ${selected.length} 个邮箱`;
  } catch (error) { errorBox.textContent = error.message; } finally { state.copyInFlight = false; }
}

el("close-result-detail")?.addEventListener("click", closeResultDetails);
el("result-reverify-button")?.addEventListener("click", reverifyActiveResult);
el("copy-deliverable-button")?.addEventListener("click", () => copyEmails("deliverable"));
el("copy-undeliverable-button")?.addEventListener("click", () => copyEmails("undeliverable"));
el("copy-unknown-button")?.addEventListener("click", () => copyEmails("unknown"));
el("copy-all-button")?.addEventListener("click", () => copyEmails("all"));

let searchTimer = null;
el("result-search")?.addEventListener("input", () => {
  clearTimeout(searchTimer);
  state.page = 0;
  searchTimer = setTimeout(() => loadResults(), 250);
});
el("result-filter")?.addEventListener("change", async () => {
  state.page = 0;
  await loadResults();
});
el("previous-page")?.addEventListener("click", async () => {
  if (state.page === 0) return;
  state.page -= 1;
  await loadResults();
});
el("next-page")?.addEventListener("click", async () => {
  if ((state.page + 1) * pageSize >= state.resultsAvailable) return;
  state.page += 1;
  await loadResults();
});
el("download-button")?.addEventListener("click", async () => {
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
el("stop-job-button")?.addEventListener("click", async () => {
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
el("resume-job-button")?.addEventListener("click", async () => {
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
        [email, "未验证", "验证后查看联系建议"].forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        });
        body.append(row);
      });
    } else {
      const row = document.createElement("tr");
      row.className = "empty-row";
      row.innerHTML = '<td colspan="3">正在生成验证结果</td>';
      body.append(row);
    }
    return;
  }
  state.discovery.results.forEach((item) => {
    const [label, className] = resultMeta(item);
    const row = document.createElement("tr");
    row.className = "result-email-row";
    [
      item.email,
      label,
      consumerResultAction(item),
    ].forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 0) {
        cell.className = "result-email-cell";
        const content = document.createElement("span"); content.className = "result-email-content";
        const valueNode = document.createElement("span"); valueNode.className = "result-email-value"; valueNode.textContent = value;
        const copy = document.createElement("button"); copy.type = "button"; copy.className = "copy-button result-copy-button";
        copy.setAttribute("aria-label", VerigoI18n.text("复制邮箱")); copy.title = VerigoI18n.text("复制邮箱");
        copy.innerHTML = '<i class="fa-regular fa-copy" aria-hidden="true"></i>';
        copy.addEventListener("click", (event) => { event.stopPropagation(); copyEmailValue(item.email, copy); });
        content.append(valueNode, copy); cell.append(content);
      } else if (index === 1) {
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
  el("discovery-title").textContent = VerigoI18n.text(`查找 ${job.total} 个候选邮箱`);
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
  el("discovery-progress-copy").textContent = VerigoI18n.text(job.qq_slow
    ? `${progressCopy}；QQ 邮箱采用低并发和自动退避策略，请耐心等待。`
    : progressCopy);
}

function updateDiscoveryVerdict(job) {
  const verdict = el("discovery-verdict");
  if (job.status === "stopped") {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = VerigoI18n.text("验证已停止，已保留当前结果。");
    return;
  }
  if (job.status !== "completed") {
    verdict.className = "discovery-verdict";
    verdict.textContent = VerigoI18n.text("正在从候选地址中确认结果");
    return;
  }
  const good = state.discovery.results.filter((item) => resultType(item) === "deliverable");
  const unknown = state.discovery.results.filter((item) => resultType(item) === "unknown");
  if (good.length === 1) {
    verdict.className = "discovery-verdict good";
    verdict.textContent = VerigoI18n.text(`已找到唯一可确认邮箱：${good[0].email}`);
  } else if (good.length > 1) {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = VerigoI18n.text(`找到 ${good.length} 个可确认地址，请结合职位或公开信息进一步确认。`);
  } else if (unknown.length) {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = VerigoI18n.text("没有可确认地址，部分候选暂时无法确认。请稍后重试或检查域名。");
  } else {
    verdict.className = "discovery-verdict warn";
    verdict.textContent = VerigoI18n.text("未找到可确认地址。请检查姓名和域名，或对方可能已离职。");
  }
}

async function loadDiscoveryResults() {
  const payload = await api(`/api/jobs/${state.discovery.jobId}/results?offset=0&limit=100`);
  state.discovery.results = payload.items;
  renderDiscoveryResults();
}

let domainPreviewTimer;
let domainPreviewController;
let domainSuggestionsController;
let domainRelationsController;
let domainPreviewRequestId = 0;
function normalizePreviewDomain(value) {
  return value.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/", 1)[0];
}
function domainLogoUrl(item) {
  const domain = item.domain || "";
  return item.logo_url || `https://logos.hunter.io/${domain}`;
}
const previewCountryNames = { DE: "Germany", NL: "Netherlands", FR: "France", IT: "Italy", ES: "Spain", BE: "Belgium", CH: "Switzerland", AT: "Austria", UK: "United Kingdom", SG: "Singapore", AE: "United Arab Emirates", AU: "Australia", ZA: "South Africa", IN: "India", CN: "China", JP: "Japan", KR: "South Korea", HK: "Hong Kong", TW: "Taiwan" };
function previewCountryName(country) {
  return previewCountryNames[String(country || "").toUpperCase()] || "Related site";
}
function previewBrandName(domain) {
  return String(domain || "").split(".")[0].replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) || "Company";
}
function createDomainPreviewRow(item, { primary = false, selectable = false } = {}) {
  const domain = item.domain || "";
  const row = document.createElement("article");
  row.className = `domain-preview-row${primary ? " is-primary" : ""}`;
  const logo = document.createElement("img");
  logo.className = "domain-preview-logo";
  logo.alt = "";
  logo.loading = "lazy";
  logo.src = domainLogoUrl(item);
  logo.onerror = () => { logo.onerror = null; logo.src = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=128`; };
  row.append(logo);
  const copy = document.createElement("div");
  copy.className = "domain-preview-copy";
  const title = document.createElement("strong");
  const legalNameUnconfirmed = item.identity_confidence === "unconfirmed" && !item.legal_name;
  title.textContent = legalNameUnconfirmed ? "法律实体名称未确认" : (item.title || item.entity || (primary ? `${previewBrandName(domain)} 官网` : "关联站点"));
  const domainText = document.createElement("span");
  domainText.textContent = domain;
  copy.append(title, domainText);
  row.append(copy);
  if (selectable) {
    const use = document.createElement("button");
    use.type = "button";
    use.className = "domain-preview-use";
    use.textContent = "使用此域名";
    use.addEventListener("click", () => {
      const input = el("discovery-domain");
      input.value = domain;
      previewDomain(domain);
    });
    use.textContent = "使用此域名";
    row.append(use);
  }
  const link = document.createElement("a");
  link.className = "domain-preview-link";
  link.href = item.url || `https://${domain}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.setAttribute("aria-label", `打开 ${domain}`);
  link.innerHTML = '<i class="fa-solid fa-arrow-up-right-from-square" aria-hidden="true"></i>';
  row.append(link);
  return row;
}
// Keep each suggestion self-contained: logo, verified name, and a clickable
// canonical URL are visible together so users can distinguish similar domains.
function createDomainPreviewRow(item, { primary = false, selectable = false } = {}) {
  const domain = item.domain || "";
  const url = item.url || `https://${domain}`;
  const row = document.createElement("article");
  row.className = `domain-preview-row${primary ? " is-primary" : ""}`;
  const logo = document.createElement("img");
  logo.className = "domain-preview-logo";
  logo.alt = item.title || domain;
  logo.loading = "lazy";
  logo.src = domainLogoUrl(item);
  logo.onerror = () => { logo.onerror = null; logo.src = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=128`; };
  row.append(logo);

  const copy = document.createElement("div");
  copy.className = "domain-preview-copy";
  const title = document.createElement("strong");
  const legalNameUnconfirmed = item.identity_confidence === "unconfirmed" && !item.legal_name;
  title.textContent = legalNameUnconfirmed ? "法律实体名称未确认" : (item.legal_name || item.title || (primary ? `${previewBrandName(domain)} 官网` : "关联站点"));
  const website = document.createElement("a");
  website.className = "domain-preview-url";
  website.href = url;
  website.target = "_blank";
  website.rel = "noopener noreferrer";
  website.textContent = `(${url})`;
  website.title = url;
  copy.append(title, website);
  row.append(copy);

  if (selectable) {
    const use = document.createElement("button");
    use.type = "button";
    use.className = "domain-preview-use";
    use.textContent = "使用此域名";
    use.addEventListener("click", () => {
      const input = el("discovery-domain");
      input.value = domain;
      previewDomain(domain);
    });
    row.append(use);
  }
  const link = document.createElement("a");
  link.className = "domain-preview-link";
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.setAttribute("aria-label", `打开 ${domain}`);
  link.innerHTML = '<i class="fa-solid fa-arrow-up-right-from-square" aria-hidden="true"></i>';
  row.append(link);
  return row;
}
function renderDomainPreviewRows(items, options = {}) {
  const list = el("domain-preview-list");
  list.replaceChildren(...items.map((item) => createDomainPreviewRow(item, options)));
}
async function previewDomain(value) {
  const card = el("domain-preview");
  const domain = normalizePreviewDomain(value);
  if (!domain) { card.classList.add("hidden"); return; }
  const requestId = ++domainPreviewRequestId;
  const status = el("domain-preview-status");
  status.textContent = "正在识别官网…";
  el("domain-preview-list").replaceChildren();
  status.textContent = "正在搜索匹配域名…";
  card.classList.remove("hidden");
  domainPreviewController?.abort();
  domainRelationsController?.abort();
  domainSuggestionsController?.abort();
  if (!domain.includes(".")) {
    domainSuggestionsController = new AbortController();
    try {
      const response = await api(`/api/domain-suggestions?q=${encodeURIComponent(domain)}`, { signal: domainSuggestionsController.signal });
      if (requestId !== domainPreviewRequestId) return;
      const suggestions = (response.suggestions || []).slice(0, 6);
      renderDomainPreviewRows(suggestions, { selectable: true });
      status.textContent = suggestions.length ? "请选择一个域名继续" : "暂未找到匹配域名";
      status.textContent = suggestions.length ? "请选择一个域名继续" : "暂未找到匹配域名";
    } catch (error) {
      if (error.name !== "AbortError" && requestId === domainPreviewRequestId) status.textContent = "域名建议暂不可用";
    }
    return;
  }
  if (domain.length < 3) { card.classList.add("hidden"); return; }
  domainPreviewController = new AbortController();
  try {
    const response = await api(`/api/domain-preview?q=${encodeURIComponent(domain)}`, { signal: domainPreviewController.signal });
    if (requestId !== domainPreviewRequestId) return;
    if (response.suggestions?.length) {
      renderDomainPreviewRows(response.suggestions, { selectable: true });
      status.textContent = "请选择一个域名继续";
      status.textContent = "请选择一个匹配的域名继续";
      return;
    }
    if (!domain.includes(".")) {
      el("domain-preview-list").replaceChildren();
      status.textContent = "没有找到已验证的匹配域名，请输入完整域名";
      return;
    }
    const primary = { domain: response.domain, url: response.url, title: response.title || `${previewBrandName(response.domain)} Official Website`, logo_url: response.logo_url };
    const cachedRelated = (response.related_domains || []).map((item, index) => ({
      ...item,
      title: item.title || response.entities?.[index] || "关联站点",
    })).slice(0, 6);
    renderDomainPreviewRows([primary, ...cachedRelated], { primary: !cachedRelated.length });
    status.textContent = cachedRelated.length ? "官网及关联站点" : "暂未发现可确认的关联站点";
    if (!response.relations_pending) {
      status.textContent = cachedRelated.length ? "官网及关联站点" : "未发现可确认的关联站点";
      return;
    }
    status.textContent = response.reachable ? "正在补充关联站点…" : "域名暂时无法访问，仍可继续提交邮箱查找";
    status.textContent = response.reachable ? "正在补充关联站点…" : "域名暂时无法访问，仍可继续提交邮箱查找";
    domainRelationsController = new AbortController();
    api(`/api/domain-relations?q=${encodeURIComponent(domain)}`, { signal: domainRelationsController.signal }).then((relations) => {
      if (requestId !== domainPreviewRequestId) return;
      const related = (relations.related_domains || []).map((item, index) => ({
        ...item,
        title: item.title || relations.entities?.[index] || "关联站点",
      })).slice(0, 6);
      if (related.length) renderDomainPreviewRows([primary, ...related]);
      status.textContent = related.length ? "官网及关联站点" : "暂未发现可确认的关联站点";
      status.textContent = related.length ? "官网及关联站点" : "未发现可确认的关联站点";
    }).catch(() => { if (requestId === domainPreviewRequestId) status.textContent = "关联站点暂不可用"; });
  } catch (error) {
    if (error.name !== "AbortError") {
      el("domain-preview-list").replaceChildren();
      status.textContent = "暂时无法识别官网，你仍可以继续提交邮箱查找";
    }
  }
}
el("discovery-domain")?.addEventListener("input", (event) => {
  clearTimeout(domainPreviewTimer);
  domainPreviewTimer = setTimeout(() => previewDomain(event.target.value), 180);
});

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

el("discovery-start")?.addEventListener("click", async () => {
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
    verifyButton.textContent = VerigoI18n.text(`免费验证候选邮箱 · ${state.discovery.candidates.length} 个地址`);
    el("discovery-title").textContent = VerigoI18n.text(`${state.discovery.candidates.length} 个候选邮箱`);
    el("discovery-status").textContent = VerigoI18n.text("已找到");
    el("discovery-status").className = "status status-completed";
    el("discovery-progress-percent").textContent = "0%";
    el("discovery-progress-bar").style.width = "0%";
    el("discovery-progress-copy").textContent = VerigoI18n.text("等待验证");
    const hasQqCandidate = state.discovery.candidates.some(isQqEmail);
    el("discovery-verdict").className = hasQqCandidate ? "discovery-verdict warn" : "discovery-verdict";
    el("discovery-verdict").textContent = VerigoI18n.text(hasQqCandidate
      ? `已生成 ${state.discovery.candidates.length} 个候选地址。QQ 邮箱验证采用专属低并发策略，验证速度较慢，请耐心等待。`
      : `已生成 ${state.discovery.candidates.length} 个候选地址`);
    renderDiscoveryResults();
  } catch (requestError) {
    error.textContent = requestError.message;
  } finally {
    button.disabled = false;
  }
});

el("discovery-verify")?.addEventListener("click", async () => {
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
el("discovery-export-button")?.addEventListener("click", () => { if (!state.discovery.jobId) return; window.location.href = `/api/jobs/${encodeURIComponent(state.discovery.jobId)}/download`; });
el("discovery-stop-button")?.addEventListener("click", async () => {
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
      empty.textContent = VerigoI18n.text("暂无任务");
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
      meta.textContent = `${VerigoI18n.text(statusLabels[job.status] || job.status)} · ${job.total}`;
      button.append(name);
      if (job.review_updated) {
        const dot = document.createElement("i");
        dot.className = "recent-job-update";
        dot.setAttribute("aria-label", VerigoI18n.text("复核结果已更新"));
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
    el("recent-jobs-page-info").textContent = `第 ${page + 1} / ${Math.max(1, Math.ceil(total / state.recentJobs.limit))} 页，共 ${total} 个任务`;
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
      const copy = document.createElement("span"); copy.className = "history-item-copy";
      const name = document.createElement("strong"); name.textContent = job.list_name || job.download_name || job.file_name || formatJobName(job.created_at);
      const meta = document.createElement("small"); meta.textContent = `${job.total || 0} 个邮箱 · ${new Date(job.created_at).toLocaleString("zh-CN")}`;
      copy.append(name, meta);
      const status = document.createElement("span"); status.className = `history-item-status history-status-${job.status}`; status.textContent = statusLabels[job.status] || job.status;
      button.append(copy, status); button.addEventListener("click", () => { switchView("single"); showJob(job); state.results = []; state.page = 0; loadResults(); }); list.append(button);
    });
    if (!(data.items || []).length) { const empty = document.createElement("p"); empty.className = "history-empty"; empty.textContent = "暂无历史任务"; list.append(empty); }
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
function scheduleNotificationPolling() {
  clearInterval(state.notificationTimer);
  state.notificationTimer = null;
  if (state.user && state.notificationPreferences.autoRefresh) {
    state.notificationTimer = window.setInterval(loadNotifications, 60000);
  }
}

function updateAccount() {
  el("account-button").textContent = state.user ? state.user.email : "登录";
  el("account-name").textContent = state.user?.email || "";
  const trialCredits = Number(state.user?.trial_credits || 0);
  el("account-credits").textContent = state.user
    ? state.user.is_admin
      ? VerigoI18n.text("无限额度")
      : VerigoI18n.locale === "en"
        ? `${state.user.credits || 0} verifications${trialCredits ? ` · ${trialCredits} trial credits` : ""}`
        : `${state.user.credits || 0} 验证次数${trialCredits ? ` · ${trialCredits} 体验次数` : ""}`
    : "";
  el("account-credits").title = state.user?.trial_credit_expires_at
    ? VerigoI18n.locale === "en"
      ? `Trial credits valid until ${VerigoI18n.formatDate(state.user.trial_credit_expires_at)}`
      : `体验额度有效至 ${VerigoI18n.formatDate(state.user.trial_credit_expires_at)}`
    : "";
  const setHidden = (id, hidden) => el(id)?.classList.toggle("hidden", hidden);
  setHidden("bind-email-button", !state.user?.needs_email_binding);
  setHidden("dashboard-nav", !state.user?.is_admin);
  setHidden("admin-credits-nav", !state.user?.is_admin);
  setHidden("system-health-nav", !state.user?.is_admin);
  setHidden("wallet-nav", !state.user);
  setHidden("workspace-nav", !state.user);
  setHidden("notification-button", !state.user);
  setHidden("claim-trial-button", !state.user || state.user.needs_email_binding || state.user.email_verified);
  setHidden("recent-block", !state.user);
  el("account-menu")?.classList.add("hidden");
  closeNotificationMenu();
  if (state.user) {
    loadRecentJobs();
    loadNotifications();
    syncOnboarding();
  }
  scheduleNotificationPolling();
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
  const loading = el("workspace-recent-jobs");
  if (loading) loading.textContent = VerigoI18n.text("正在加载任务…");
  const credits = Number(state.user.credits || 0) + Number(state.user.trial_credits || 0);
  el("workspace-credits").textContent = credits.toLocaleString("zh-CN");
  try {
    const data = await api("/api/workspace");
    const jobs = data.items || [];
    const locale = VerigoI18n.locale === "en" ? "en-US" : "zh-CN";
    el("workspace-job-count").textContent = Number(data.total || jobs.length).toLocaleString(locale);
    el("workspace-processed").textContent = Number(data.processed_today || 0).toLocaleString(locale);
    el("workspace-deliverable-rate").textContent = Number(data.settled || 0) ? `${Math.round(Number(data.deliverable || 0) / Number(data.settled) * 100)}%` : "—";
    const list = el("workspace-recent-jobs"); list.replaceChildren();
    const recentResults = el("workspace-recent-results");
    if (recentResults) { recentResults.replaceChildren(); (data.recent_results || []).forEach((result) => { const item = document.createElement("button"); item.type = "button"; item.className = "workspace-job-row"; const name = document.createElement("strong"); name.textContent = result.email; const meta = document.createElement("span"); meta.textContent = `${VerigoI18n.text(result.status)} · ${VerigoI18n.formatDate(result.created_at)}`; item.append(name, meta); item.addEventListener("click", () => openResultDetails(result)); recentResults.append(item); }); if (!recentResults.children.length) { const empty = document.createElement("p"); empty.className = "workspace-empty"; empty.textContent = VerigoI18n.text("还没有最近验证结果"); recentResults.append(empty); } }
    if (!jobs.length) { const empty = document.createElement("p"); empty.className = "workspace-empty"; empty.textContent = VerigoI18n.text("还没有任务，先验证一个邮箱吧。"); list.append(empty); }
    jobs.slice(0, 5).forEach((job) => {
      const item = document.createElement("button"); item.type = "button"; item.className = "workspace-job-row";
      const name = document.createElement("strong");
      name.textContent = formatJobName(job.created_at);
      const meta = document.createElement("span");
      meta.textContent = `${VerigoI18n.text(statusLabels[job.status] || job.status)} · ${Number(job.total || 0).toLocaleString(locale)} ${VerigoI18n.text("个邮箱")}`;
      item.append(name, meta);
      item.addEventListener("click", () => { switchView("single"); showJob(job); state.results = []; state.page = 0; loadResults(); }); list.append(item);
    });
  } catch (error) { el("workspace-recent-jobs").textContent = `${VerigoI18n.text("任务加载失败，请稍后刷新。")} ${error.message || ""}`; }
}

/* Lists were intentionally retired from the product UI. */
/* Retired list workspace kept out of the UI. */
el("dashboard-refresh")?.addEventListener("click", loadDashboardMetrics);
async function loadWallet() { const data = await api("/api/wallet"); const set=(id,v)=>el(id).textContent=Number(v||0).toLocaleString("zh-CN"); set("wallet-available",data.available_verifications); el("wallet-paid").textContent=`${Number(data.paid_verifications||0).toLocaleString("zh-CN")} 次`; el("wallet-used").textContent=`${Number(data.paid_verifications_used||0).toLocaleString("zh-CN")} 次`; el("wallet-recharged").textContent=`¥${(Number(data.cumulative_recharge_fen||0)/100).toFixed(2)}`; el("wallet-value").textContent=`¥${Number(data.remaining_paid_value_yuan||0).toFixed(2)}`; el("wallet-spent").textContent=`¥${Number(data.paid_used_value_yuan||0).toFixed(2)}`; el("wallet-price").textContent=`100 次 ¥${(data.price_fen_per_100/100).toFixed(2)}`; el("wallet-trial-note").textContent=data.trial_verifications?`另有 ${data.trial_verifications} 体验次数`:"不含体验次数"; el("wallet-updated").textContent=`更新于 ${new Date().toLocaleString("zh-CN")}`; const days=data.usage_daily||[]; const max=Math.max(1,...days.map(x=>x.verifications)); el("wallet-usage-chart").innerHTML=days.map(x=>`<div class="wallet-bar" style="height:${Math.max(4,x.verifications/max*180)}px"><span>${x.verifications}</span></div>`).join(""); el("wallet-transactions").innerHTML=(data.transactions||[]).map(x=>`<div class="wallet-transaction"><div><strong>${x.title}</strong><small>${x.credits>0?"+":""}${x.credits} 次 ${x.note||""}</small></div><div><strong>${x.amount_fen==null?"—":`${x.credits<0?"-":"+"}¥${(x.amount_fen/100).toFixed(2)}`}</strong><small>${new Date(x.created_at).toLocaleString("zh-CN")}</small></div></div>`).join("")||"暂无资金流水"; }
el("wallet-refresh")?.addEventListener("click", loadWallet);

function focusRedemptionForm() {
  window.setTimeout(() => {
    el("wallet-redemption").scrollIntoView({ behavior: "smooth", block: "center" });
    el("redemption-code").focus({ preventScroll: true });
  }, 0);
}

el("redeem-nav")?.addEventListener("click", () => {
  el("account-menu").classList.add("hidden");
  if (!state.user) {
    state.pendingView = "wallet";
    state.pendingRedemption = true;
    setAuthMode("login");
    el("auth-error").textContent = "请先登录后兑换验证额度";
    el("auth-dialog").showModal();
    return;
  }
  switchView("wallet");
  focusRedemptionForm();
});

el("redemption-code")?.addEventListener("input", (event) => {
  event.currentTarget.value = event.currentTarget.value.toUpperCase();
});
el("redemption-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = el("redemption-submit");
  const status = el("redemption-status");
  submit.disabled = true;
  status.className = "purchase-status";
  status.textContent = "正在兑换…";
  try {
    const result = await api("/api/wallet/redeem", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: el("redemption-code").value }),
    });
    status.classList.add("success");
    status.textContent = `兑换成功：${result.credits.toLocaleString("zh-CN")} 个邮箱额度已到账，当前可用 ${result.available_credits.toLocaleString("zh-CN")} 个。`;
    el("redemption-code").value = "";
    state.user.credits = result.available_credits;
    state.user.paid_credits = Number(state.user.paid_credits || 0) + result.credits;
    updateAccount();
    await Promise.all([loadWallet(), loadNotifications()]);
  } catch (error) {
    status.classList.add("error");
    status.textContent = error.message;
  } finally { submit.disabled = false; }
});

el("admin-redemption-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = el("admin-redemption-submit");
  const result = el("admin-redemption-result");
  submit.disabled = true;
  result.className = "admin-redemption-result";
  result.replaceChildren();
  try {
    const created = await api("/api/admin/redemption-codes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        amount_yuan: Number(el("admin-redemption-amount").value),
        quantity: Number(el("admin-redemption-quantity").value),
      }),
    });
    const heading = document.createElement("strong");
    heading.textContent = `已生成 ${created.codes.length} 个 ¥${created.amount_yuan} 兑换码，每个含 ${created.credits.toLocaleString("zh-CN")} 个邮箱额度。请立即保存：`;
    const list = document.createElement("div");
    list.className = "admin-redemption-code-list";
    created.codes.forEach((code) => {
      const item = document.createElement("code");
      item.className = "admin-redemption-code";
      item.textContent = code;
      list.append(item);
    });
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "admin-redemption-copy";
    copy.textContent = "复制全部兑换码";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(created.codes.join("\n"));
        copy.textContent = "已复制";
      } catch (_) { copy.textContent = "复制失败，请手动选择"; }
    });
    result.append(heading, list, copy);
  } catch (error) {
    result.classList.add("error");
    result.textContent = error.message;
  } finally { submit.disabled = false; }
});

const companyCatalogState = { offset: 0, limit: 25, total: 0, hasMore: false };
function companyCatalogQuery() {
  const params = new URLSearchParams({ offset: String(companyCatalogState.offset), limit: String(companyCatalogState.limit) });
  const fields = {
    query: el("company-catalog-query")?.value,
    country: el("company-catalog-country")?.value,
    industry: el("company-catalog-industry")?.value,
    size: el("company-catalog-size")?.value,
    has_website: el("company-catalog-website")?.value,
  };
  Object.entries(fields).forEach(([key, value]) => { if (value) params.set(key, value.trim()); });
  return params;
}
async function loadCompanyCatalog() {
  const status = el("company-catalog-status");
  const body = el("company-catalog-results");
  if (!status || !body) return;
  status.className = "admin-credit-result";
  status.textContent = "正在查询公司目录…";
  try {
    const data = await api(`/api/admin/company-catalog/search?${companyCatalogQuery()}`);
    companyCatalogState.total = Number(data.total || 0);
    companyCatalogState.hasMore = Boolean(data.has_more);
    body.replaceChildren();
    (data.items || []).forEach((item) => {
      const row = document.createElement("tr");
      const companyCell = document.createElement("td");
      const company = document.createElement("span"); company.className = "company-catalog-company";
      if (item.logo_url) { const logo = document.createElement("img"); logo.src = item.logo_url; logo.alt = ""; logo.width = 28; logo.height = 28; logo.loading = "lazy"; logo.addEventListener("error", () => logo.remove()); company.append(logo); }
      const name = document.createElement("span"); name.textContent = item.name || "-"; company.append(name); companyCell.append(company); row.append(companyCell);
      const cells = [[item.country, item.region, item.locality].filter(Boolean).join(" / "), item.industry || "-", item.size || "-", item.website || "-"];
      cells.forEach((value) => { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); });
      const linkedinCell = document.createElement("td");
      if (item.linkedin_url) { const link = document.createElement("a"); link.href = item.linkedin_url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.className = "company-catalog-linkedin"; link.textContent = "Open"; linkedinCell.append(link); } else { linkedinCell.textContent = "-"; }
      row.append(linkedinCell);
      body.append(row);
    });
    if (!body.children.length) { const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 6; cell.textContent = "没有匹配的公司"; row.append(cell); body.append(row); }
    const page = Math.floor(companyCatalogState.offset / companyCatalogState.limit) + 1;
    const totalLabel = companyCatalogState.hasMore ? `至少 ${companyCatalogState.total.toLocaleString("zh-CN")}` : companyCatalogState.total.toLocaleString("zh-CN");
    el("company-catalog-page").textContent = `第 ${page} 页（${totalLabel} 家）`;
    el("company-catalog-prev").disabled = companyCatalogState.offset === 0;
    el("company-catalog-next").disabled = !companyCatalogState.hasMore;
    status.classList.add("success"); status.textContent = "查询完成";
  } catch (error) {
    status.classList.add("error"); status.textContent = error.message;
  }
}
el("company-catalog-form")?.addEventListener("submit", (event) => { event.preventDefault(); companyCatalogState.offset = 0; loadCompanyCatalog(); });
el("company-catalog-prev")?.addEventListener("click", () => { companyCatalogState.offset = Math.max(0, companyCatalogState.offset - companyCatalogState.limit); loadCompanyCatalog(); });
el("company-catalog-next")?.addEventListener("click", () => { companyCatalogState.offset += companyCatalogState.limit; loadCompanyCatalog(); });

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
      ? "验证已完成，正在更新账户。"
      : `正在验证：${job.completed} / ${job.total}`;
    if (job.status === "completed") {
      state.user = await api("/api/auth/onboarding/activation/complete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: job.id }),
      });
      showOnboardingStep("completed");
      return;
    }
    if (job.status === "failed" || job.status === "stopped") {
      el("onboarding-job-status").textContent = "本次验证未完成，请关闭窗口后重新提交一个邮箱。";
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
el("onboarding-email-form")?.addEventListener("submit", async (event) => {
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
el("onboarding-resend")?.addEventListener("click", async () => {
  const button = el("onboarding-resend"); button.disabled = true; el("onboarding-email-error").textContent = "";
  try { await api("/api/auth/email-verification/request", { method: "POST" }); el("onboarding-email-error").textContent = "新的验证码已发送。"; }
  catch (error) { el("onboarding-email-error").textContent = error.message; } finally { button.disabled = false; }
});
el("onboarding-check-form")?.addEventListener("submit", async (event) => {
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
el("onboarding-go-wallet")?.addEventListener("click", () => { el("onboarding-dialog").close(); switchView("wallet"); });
el("onboarding-finish")?.addEventListener("click", () => el("onboarding-dialog").close());

function refreshPurchasePrice() {
  const packages = Math.max(1, Math.min(1000, Number(el("purchase-packages").value) || 1));
  el("purchase-packages").value = String(packages);
  el("purchase-button").textContent = `购买 ${(packages * 100).toLocaleString("zh-CN")} 次 · ¥${(packages * 0.5).toFixed(2)}`;
}
el("purchase-packages")?.addEventListener("input", refreshPurchasePrice);
el("close-purchase")?.addEventListener("click", () => el("purchase-dialog").close());
el("purchase-button")?.addEventListener("click", async () => {
  const button = el("purchase-button"); button.disabled = true; el("purchase-status").className = "purchase-status"; el("purchase-status").textContent = "";
  try {
    const order = await api("/api/billing/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ packages: Number(el("purchase-packages").value) }) });
    el("purchase-dialog-copy").textContent = `订单 ${order.id.slice(0, 12)} 已创建：${order.credits.toLocaleString("zh-CN")} 次验证额度，共 ¥${(order.amount_fen / 100).toFixed(2)}。`;
    const actions = el("purchase-dialog-actions"); actions.replaceChildren();
    if (!order.checkout_url) throw new Error("支付通道暂未配置，请稍后再试");
    const pay = document.createElement("a"); pay.className = "primary-action"; pay.href = order.checkout_url; pay.textContent = "前往安全支付"; actions.append(pay);
    el("purchase-dialog-status").textContent = "支付成功后，额度会自动到账。"; el("purchase-dialog").showModal();
  } catch (error) { el("purchase-status").className = "purchase-status error"; el("purchase-status").textContent = error.message; } finally { button.disabled = false; }
});
async function loadAdminAccounts(){try{const data=await api(`/api/admin/accounts/list?offset=${state.adminAccountOffset}&limit=50`),rows=data.items,summary=data.summary||{};el("admin-metric-users").textContent=data.total.toLocaleString("zh-CN");el("admin-metric-paid").textContent=Number(summary.paid_verifications||0).toLocaleString("zh-CN");el("admin-metric-trial").textContent=Number(summary.trial_verifications||0).toLocaleString("zh-CN");el("admin-metric-used").textContent=Number(summary.used_verifications||0).toLocaleString("zh-CN");el("admin-accounts-meta").textContent=`共 ${data.total} 个账户，按注册时间排序`;el("admin-accounts-list").innerHTML=rows.map(r=>`<button class="admin-account-row" data-email="${r.email}" type="button"><strong>${r.email}</strong><span>付费 ${r.paid_verifications}</span><span>体验 ${r.trial_verifications}</span><span>已用 ${r.used_verifications}</span></button>`).join("")||"暂无账户";el("admin-accounts-page").textContent=`${data.offset+1}-${Math.min(data.offset+data.limit,data.total)} / ${data.total}`;el("admin-accounts-prev").disabled=!data.offset;el("admin-accounts-next").disabled=data.offset+data.limit>=data.total;const page=Math.floor(data.offset/data.limit);renderPageNumbers(el("admin-accounts-pages"),page,Math.max(1,Math.ceil(data.total/data.limit)),nextPage=>{state.adminAccountOffset=nextPage*data.limit;loadAdminAccounts();});document.querySelectorAll(".admin-account-row").forEach(b=>b.addEventListener("click",()=>{el("admin-credit-email").value=b.dataset.email;el("admin-account-lookup").click();}));}catch(error){["admin-metric-users","admin-metric-paid","admin-metric-trial","admin-metric-used"].forEach(id=>el(id).textContent="—");el("admin-accounts-meta").textContent=`账户数据加载失败：${error.message}`;el("admin-accounts-list").textContent="请刷新后重试";}}
async function loadAdminFeatureUsage(){const data=await api("/api/admin/feature-usage");const days=data.daily||[];const width=620,height=350,p={top:18,right:12,bottom:30,left:30},max=Math.max(1,...days.flatMap(day=>[day.single,day.batch,day.discovery]));const x=index=>p.left+(days.length>1?index*(width-p.left-p.right)/(days.length-1):(width-p.left-p.right)/2),point=(value,index)=>`${x(index)},${p.top+(height-p.top-p.bottom)*(1-value/max)}`;const series=[["single","single"],["batch","batch"],["discovery","discovery"]];const grid=[0,.5,1].map(step=>{const y=p.top+(height-p.top-p.bottom)*step;return `<line class="admin-feature-grid" x1="${p.left}" y1="${y}" x2="${width-p.right}" y2="${y}"/><text class="admin-feature-axis" x="0" y="${y+4}">${Math.round(max*(1-step))}</text>`;}).join("");const labels=days.map((day,index)=>index%2&&days.length>8?"":`<text class="admin-feature-axis" text-anchor="middle" x="${x(index)}" y="${height-8}">${day.day.slice(5).replace("-","/")}</text>`).join("");const lines=series.map(([key,name])=>`<polyline class="admin-feature-line-${name}" points="${days.map((day,index)=>point(day[key],index)).join(" ")}" fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>${days.map((day,index)=>{const [px,py]=point(day[key],index).split(",");return `<circle cx="${px}" cy="${py}" r="3" fill="currentColor" class="admin-feature-line-${name}"><title>${day.day} ${key} ${day[key]}</title></circle>`;}).join("")}`).join("");el("admin-feature-chart").innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="功能使用趋势">${grid}${lines}${labels}</svg>`;el("admin-feature-legend").innerHTML=`<span>单个 ${data.totals.single}</span><span>批量 ${data.totals.batch}</span><span>查找 ${data.totals.discovery}</span>`;}
el("admin-accounts-refresh")?.addEventListener("click",()=>{state.adminAccountOffset=0;loadAdminAccounts();});el("admin-accounts-prev")?.addEventListener("click",()=>{state.adminAccountOffset=Math.max(0,state.adminAccountOffset-50);loadAdminAccounts();});el("admin-accounts-next")?.addEventListener("click",()=>{state.adminAccountOffset+=50;loadAdminAccounts();});
el("admin-account-lookup")?.addEventListener("click", async()=>{const email=el("admin-credit-email").value;if(!email){el("admin-credit-result").className="admin-credit-result error";el("admin-credit-result").textContent="请输入邮箱地址";return;}const btn=el("admin-account-lookup");const originalText=btn.innerHTML;btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i>查询中';el("admin-credit-result").className="admin-credit-result";el("admin-credit-result").textContent="";try{const account=await api(`/api/admin/accounts?email=${encodeURIComponent(email)}`);el("account-info-paid").textContent=account.paid_verifications||0;el("account-info-trial").textContent=account.trial_verifications||0;el("account-info-used").textContent=account.used_verifications||0;el("admin-account-info").classList.remove("hidden");el("admin-credit-result").className="admin-credit-result success";el("admin-credit-result").textContent=`找到账户：${account.email}`;}catch(error){el("admin-account-info").classList.add("hidden");el("admin-credit-result").className="admin-credit-result error";el("admin-credit-result").textContent=error.message;}finally{btn.disabled=false;btn.innerHTML=originalText;}});
el("admin-credit-action")?.addEventListener("change",()=>{const action=el("admin-credit-action").value;const btn=el("admin-credit-submit");btn.innerHTML=action==="deduct"?'<i class="fa-solid fa-minus"></i>确认扣减':'<i class="fa-solid fa-check"></i>确认授予';});
el("system-health-refresh")?.addEventListener("click",()=>loadSystemHealth());
async function loadSystemHealth(){try{const data=await api("/api/admin/system-health");const services=data.services||[];const workers=data.workers||[];const queue=data.queue||{};const database=data.database||{};const resources=data.resources||{};const servicesOk=services.filter(s=>s.status==="running").length;const workersActive=workers.filter(w=>w.status==="active").length;function getProgressLevel(percent){return percent>=85?"level-danger":percent>=70?"level-warn":"level-good";}el("health-services-status").textContent=`${servicesOk}/${services.length}`;el("health-services-detail").textContent=servicesOk===services.length?"全部正常":"部分异常";el("health-workers-status").textContent=`${workersActive}/${workers.length}`;el("health-workers-detail").textContent=workersActive===workers.length?"全部活跃":"部分停止";el("health-queue-pending").textContent=(queue.pending||0).toLocaleString("zh-CN");el("health-queue-running").textContent=`运行中 ${queue.running||0}`;el("health-db-connections").textContent=`${database.active||0}/${database.max||0}`;el("health-db-detail").textContent=database.idle_in_transaction>0?`${database.idle_in_transaction} 空闲事务`:"连接正常";el("health-services-list").innerHTML=services.map(s=>`<div class="health-item-row"><div class="health-item-name"><i class="fa-solid fa-${s.status==="running"?"circle-check":"circle-xmark"}"></i><strong>${s.name}</strong></div><span class="health-status-badge status-${s.status==="running"?"running":"stopped"}"><i class="fa-solid fa-${s.status==="running"?"play":"stop"}"></i>${s.status==="running"?"运行中":"已停止"}</span></div>`).join("")||"<div style=\"padding:24px;text-align:center;color:#80868b;\">无数据</div>";el("health-workers-list").innerHTML=workers.map(w=>`<div class="health-item-row"><div class="health-item-name"><i class="fa-solid fa-robot"></i><strong>${w.name||w.id||"未知"}</strong></div><div class="health-item-meta"><small>处理 ${w.processed||0} 个</small><span class="health-status-badge status-${w.status==="active"?"active":"inactive"}"><i class="fa-solid fa-${w.status==="active"?"bolt":"pause"}"></i>${w.status==="active"?"活跃":w.status}</span></div></div>`).join("")||"<div style=\"padding:24px;text-align:center;color:#80868b;\">无数据</div>";const cpuPercent=resources.cpu_percent||0;const memPercent=resources.memory_percent||0;const diskPercent=resources.disk_percent||0;el("health-resources-list").innerHTML=`<div class="health-metric-card"><div class="health-metric-icon icon-cpu"><i class="fa-solid fa-microchip"></i></div><div class="health-metric-content"><span class="health-metric-label">CPU 使用率</span><span class="health-metric-value">${cpuPercent}%</span><div class="health-progress-bar"><div class="health-progress-fill ${getProgressLevel(cpuPercent)}" style="width:${cpuPercent}%"></div></div></div></div><div class="health-metric-card"><div class="health-metric-icon icon-memory"><i class="fa-solid fa-memory"></i></div><div class="health-metric-content"><span class="health-metric-label">内存使用率</span><span class="health-metric-value">${memPercent}%</span><div class="health-metric-detail">${resources.memory_used||"—"} / ${resources.memory_total||"—"}</div><div class="health-progress-bar"><div class="health-progress-fill ${getProgressLevel(memPercent)}" style="width:${memPercent}%"></div></div></div></div><div class="health-metric-card"><div class="health-metric-icon icon-disk"><i class="fa-solid fa-hard-drive"></i></div><div class="health-metric-content"><span class="health-metric-label">磁盘使用率</span><span class="health-metric-value">${diskPercent}%</span><div class="health-metric-detail">${resources.disk_used||"—"} / ${resources.disk_total||"—"}</div><div class="health-progress-bar"><div class="health-progress-fill ${getProgressLevel(diskPercent)}" style="width:${diskPercent}%"></div></div></div></div>`;el("health-database-list").innerHTML=`<div class="health-item-row"><div class="health-item-name"><i class="fa-solid fa-link"></i><strong>活跃连接</strong></div><span style="font-weight:700;font-size:16px;color:#1a73e8;">${database.active||0}</span></div><div class="health-item-row"><div class="health-item-name"><i class="fa-solid fa-pause"></i><strong>空闲连接</strong></div><span style="font-weight:600;font-size:15px;color:#5f6368;">${database.idle||0}</span></div><div class="health-item-row"><div class="health-item-name"><i class="fa-solid fa-hourglass-half"></i><strong>空闲事务</strong></div><span class="health-status-badge status-${database.idle_in_transaction>0?"inactive":"active"}">${database.idle_in_transaction||0}</span></div><div class="health-item-row"><div class="health-item-name"><i class="fa-solid fa-database"></i><strong>最大连接数</strong></div><span style="font-weight:600;font-size:15px;color:#5f6368;">${database.max||0}</span></div><div class="health-item-row"><div class="health-item-name"><i class="fa-solid fa-list"></i><strong>待处理任务</strong></div><div class="health-item-meta"><small>运行中 ${queue.running||0}</small><span style="font-weight:700;font-size:16px;color:#e37400;">${queue.pending||0}</span></div></div>`;}catch(error){el("health-services-status").textContent="—";el("health-services-detail").textContent="加载失败";el("health-workers-status").textContent="—";el("health-workers-detail").textContent=error.message;el("health-queue-pending").textContent="—";el("health-queue-running").textContent="—";el("health-db-connections").textContent="—";el("health-db-detail").textContent="请刷新重试";}}
function notificationUiText(zh, en) {
  return VerigoI18n.locale === "en" ? en : zh;
}

function notificationPresentation(notification) {
  if (notification.kind === "verification_review") return {
    icon: "fa-solid fa-rotate", tone: "review", action: notificationUiText("查看结果", "View result"), destination: "result",
  };
  if (notification.kind === "payment") return {
    icon: "fa-solid fa-receipt", tone: "payment", action: notificationUiText("查看账单", "View billing"), destination: "wallet",
  };
  if (notification.kind === "credit_grant" || notification.kind === "credit_deduction") return {
    icon: "fa-solid fa-coins", tone: "credit", action: notificationUiText("查看额度", "View credits"), destination: "wallet",
  };
  return { icon: "fa-regular fa-bell", tone: "info", action: "", destination: null };
}

function notificationMatchesFilter(notification, filter = state.notificationFilter) {
  if (filter === "unread") return !notification.read_at;
  if (filter === "verification") return notification.kind === "verification_review";
  if (filter === "account") return ["payment", "credit_grant", "credit_deduction"].includes(notification.kind);
  return true;
}

function notificationFilterText(filter) {
  const labels = {
    all: notificationUiText("全部", "All"),
    unread: notificationUiText("未读", "Unread"),
    verification: notificationUiText("验证", "Verification"),
    account: notificationUiText("账户", "Account"),
  };
  return labels[filter] || labels.all;
}

function notificationDateGroup(value) {
  const date = new Date(value); const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.round((start - target) / 86400000);
  const key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  if (days === 0) return { key, label: notificationUiText("今天", "Today") };
  if (days === 1) return { key, label: notificationUiText("昨天", "Yesterday") };
  return { key, label: new Intl.DateTimeFormat(VerigoI18n.locale === "en" ? "en-US" : "zh-CN", { month: "short", day: "numeric" }).format(date) };
}

function applyNotificationPreferences() {
  el("notification-menu").classList.toggle("is-compact", state.notificationPreferences.compact);
  el("notification-compact").checked = state.notificationPreferences.compact;
  el("notification-auto-refresh").checked = state.notificationPreferences.autoRefresh;
}

function updateNotificationChrome() {
  const count = el("notification-count");
  count.textContent = state.notificationUnread > 99 ? "99+" : String(state.notificationUnread);
  count.classList.toggle("hidden", !state.notificationUnread);
  el("notification-summary").textContent = state.notificationUnread
    ? notificationUiText(`${state.notificationUnread} 条未读`, `${state.notificationUnread} unread`)
    : notificationUiText("没有未读通知", "No unread notifications");
  el("notification-mark-all").disabled = state.notificationLoading || !state.notificationUnread;
  el("notification-heading-title").textContent = notificationUiText("通知", "Notifications");
  el("notification-mark-all").textContent = notificationUiText("全部已读", "Mark all read");
  el("notification-menu").setAttribute("aria-label", notificationUiText("通知中心", "Notification center"));
  el("notification-close").setAttribute("aria-label", notificationUiText("关闭通知", "Close notifications"));
  el("notification-close").title = notificationUiText("关闭通知", "Close notifications");
  el("notification-refresh").setAttribute("aria-label", notificationUiText("刷新通知", "Refresh notifications"));
  el("notification-refresh").title = notificationUiText("刷新通知", "Refresh notifications");
  el("notification-refresh").disabled = state.notificationLoading;
  el("notification-settings").setAttribute("aria-label", notificationUiText("通知设置", "Notification settings"));
  el("notification-settings").title = notificationUiText("通知设置", "Notification settings");
  el("notification-settings").setAttribute("aria-expanded", String(!el("notification-preferences").classList.contains("hidden")));
  const preferenceTitle = el("notification-preferences").querySelector(".notification-preferences-title");
  preferenceTitle.querySelector("strong").textContent = notificationUiText("通知设置", "Notification settings");
  preferenceTitle.querySelector("small").textContent = notificationUiText("仅保存在当前浏览器", "Saved in this browser only");
  const preferenceRows = el("notification-preferences").querySelectorAll(".notification-preference span");
  preferenceRows[0].querySelector("strong").textContent = notificationUiText("紧凑显示", "Compact view");
  preferenceRows[0].querySelector("small").textContent = notificationUiText("隐藏正文，减少列表占用空间", "Hide message previews to fit more notifications");
  preferenceRows[1].querySelector("strong").textContent = notificationUiText("自动刷新", "Auto refresh");
  preferenceRows[1].querySelector("small").textContent = notificationUiText("每分钟检查一次新通知", "Check for new notifications every minute");
  el("notification-reset-preferences").textContent = notificationUiText("恢复默认设置", "Restore defaults");
  el("notification-filters").setAttribute("aria-label", notificationUiText("通知筛选", "Notification filters"));
  document.querySelectorAll("[data-notification-filter]").forEach((button) => {
    const active = button.dataset.notificationFilter === state.notificationFilter;
    button.textContent = notificationFilterText(button.dataset.notificationFilter);
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  const footer = el("notification-footer");
  footer.classList.toggle("hidden", !state.notificationTotal);
  el("notification-loaded-summary").textContent = notificationUiText(
    `已加载 ${state.notifications.length} / ${state.notificationTotal}`,
    `${state.notifications.length} of ${state.notificationTotal} loaded`,
  );
  applyNotificationPreferences();
}

function closeNotificationMenu() {
  el("notification-menu")?.classList.add("hidden");
  el("notification-preferences")?.classList.add("hidden");
  el("notification-button")?.setAttribute("aria-expanded", "false");
  el("notification-settings")?.setAttribute("aria-expanded", "false");
}

function setNotificationFeedback(message = "", retry = false) {
  const feedback = el("notification-feedback");
  feedback.replaceChildren();
  feedback.classList.toggle("hidden", !message);
  if (!message) return;
  const copy = document.createElement("span"); copy.textContent = message; feedback.append(copy);
  if (retry) {
    const button = document.createElement("button"); button.type = "button";
    button.textContent = notificationUiText("重试", "Retry");
    button.addEventListener("click", loadNotifications); feedback.append(button);
  }
}

async function markNotificationRead(notification) {
  if (notification.read_at) return;
  notification.read_at = new Date().toISOString();
  state.notificationUnread = Math.max(0, state.notificationUnread - 1);
  updateNotificationChrome(); renderNotifications();
  try {
    await api(`/api/notifications/${notification.id}/read`, { method: "POST" });
  } catch (error) {
    notification.read_at = null;
    state.notificationUnread += 1;
    updateNotificationChrome(); renderNotifications();
    throw error;
  }
}

async function openNotification(notification) {
  const presentation = notificationPresentation(notification);
  try {
    await markNotificationRead(notification);
    if (presentation.destination === "wallet") {
      closeNotificationMenu(); switchView("wallet"); return;
    }
    if (presentation.destination !== "result" || !notification.target_job_id || !Number.isInteger(notification.target_result_index)) return;
    closeNotificationMenu();
    switchView("single");
    el("result-search").value = ""; el("result-filter").value = "all";
    state.jobId = notification.target_job_id;
    state.guestToken = null;
    state.page = Math.floor(notification.target_result_index / pageSize);
    sessionStorage.setItem("verigo_job_id", state.jobId);
    const job = await api(`/api/jobs/${state.jobId}`);
    showJob(job); await loadResults();
    const result = state.results.find((item) => Number(item.original_index) === notification.target_result_index);
    if (result) openResultDetails(result);
    await api(`/api/jobs/${state.jobId}/results/${notification.target_result_index}/reviewed`, { method: "POST" });
  } catch (error) {
    const message = error.message || notificationUiText("通知操作失败", "Notification action failed");
    errorBox.textContent = message;
    setNotificationFeedback(message, true);
  }
}

function renderNotifications() {
  const list = el("notification-list");
  list.replaceChildren(); updateNotificationChrome();
  const visibleNotifications = state.notifications.filter((notification) => notificationMatchesFilter(notification));
  if (!visibleNotifications.length) {
    const empty = document.createElement("div"); empty.className = "notification-empty";
    empty.innerHTML = '<i class="fa-regular fa-bell" aria-hidden="true"></i>';
    const filtered = state.notificationFilter !== "all";
    const title = document.createElement("strong"); title.textContent = filtered
      ? notificationUiText(`暂无${notificationFilterText(state.notificationFilter)}通知`, `No ${notificationFilterText(state.notificationFilter).toLowerCase()} notifications`)
      : notificationUiText("暂无通知", "No notifications");
    const copy = document.createElement("span"); copy.textContent = filtered
      ? notificationUiText("可以切换其他分类查看通知。", "Switch categories to view other notifications.")
      : notificationUiText("重要的验证与账户更新会出现在这里。", "Verification and account updates will appear here.");
    empty.append(title, copy); list.append(empty); scheduleNotificationAutoLoad(); return;
  }
  let previousGroup = null;
  visibleNotifications.forEach((notification) => {
    const group = notificationDateGroup(notification.created_at);
    if (group.key !== previousGroup) {
      const heading = document.createElement("div"); heading.className = "notification-date-group"; heading.textContent = group.label;
      list.append(heading); previousGroup = group.key;
    }
    const presentation = notificationPresentation(notification);
    const item = document.createElement("button"); item.type = "button";
    item.className = `notification-item notification-${presentation.tone}${notification.read_at ? " is-read" : " is-unread"}`;
    item.addEventListener("click", () => openNotification(notification));
    const icon = document.createElement("span"); icon.className = "notification-icon";
    icon.innerHTML = `<i class="${presentation.icon}" aria-hidden="true"></i>`;
    const content = document.createElement("span"); content.className = "notification-copy";
    const title = document.createElement("strong"); title.textContent = VerigoI18n.notificationTitle(notification);
    const body = document.createElement("span"); body.className = "notification-body"; body.textContent = VerigoI18n.notificationBody(notification);
    const meta = document.createElement("span"); meta.className = "notification-meta";
    const time = document.createElement("time"); time.textContent = VerigoI18n.formatDate(notification.created_at);
    meta.append(time);
    if (presentation.action) { const action = document.createElement("span"); action.className = "notification-action"; action.textContent = presentation.action; meta.append(action); }
    content.append(title, body, meta);
    const unread = document.createElement("span"); unread.className = "notification-unread-dot"; unread.setAttribute("aria-hidden", "true");
    item.append(icon, content, unread); list.append(item);
  });
  scheduleNotificationAutoLoad();
}

function scheduleNotificationAutoLoad() {
  window.requestAnimationFrame(() => {
    const menu = el("notification-menu"); const list = el("notification-list");
    if (menu.classList.contains("hidden") || state.notificationLoading || state.notifications.length >= state.notificationTotal) return;
    if (list.scrollHeight - list.clientHeight <= 48) loadNotifications({ append: true });
  });
}

window.addEventListener("verigo:localechange", renderNotifications);

async function loadNotifications({ append = false } = {}) {
  if (!state.user || state.notificationLoading) return;
  if (append && state.notifications.length >= state.notificationTotal) return;
  state.notificationLoading = true; updateNotificationChrome(); setNotificationFeedback();
  if (!append && !state.notifications.length) {
    el("notification-list").innerHTML = `<div class="notification-loading"><i class="fa-solid fa-circle-notch fa-spin" aria-hidden="true"></i><span>${notificationUiText("正在加载通知", "Loading notifications")}</span></div>`;
  }
  try {
    const offset = append ? state.notifications.length : 0;
    const payload = await api(`/api/notifications?offset=${offset}&limit=${state.notificationLimit}`);
    const items = payload.items || [];
    if (append) {
      const existing = new Set(state.notifications.map((notification) => notification.id));
      state.notifications.push(...items.filter((notification) => !existing.has(notification.id)));
    } else {
      state.notifications = items;
    }
    state.notificationOffset = state.notifications.length;
    state.notificationUnread = Number(payload.unread_count || 0);
    state.notificationTotal = Number(payload.total || state.notifications.length);
    renderNotifications();
  } catch (error) {
    setNotificationFeedback(error.message || notificationUiText("通知加载失败", "Unable to load notifications"), true);
    if (!state.notifications.length) renderNotifications();
  } finally {
    state.notificationLoading = false; updateNotificationChrome();
  }
}

el("notification-button")?.addEventListener("click", async () => {
  const menu = el("notification-menu");
  const opening = menu.classList.contains("hidden");
  if (!opening) { closeNotificationMenu(); return; }
  menu.classList.remove("hidden");
  el("notification-button").setAttribute("aria-expanded", "true");
  el("account-menu").classList.add("hidden");
  await loadNotifications();
});
el("notification-close")?.addEventListener("click", closeNotificationMenu);
document.querySelectorAll("[data-notification-filter]").forEach((button) => button.addEventListener("click", () => {
  state.notificationFilter = button.dataset.notificationFilter;
  localStorage.setItem("verigo_notification_filter", state.notificationFilter);
  renderNotifications();
}));
el("notification-list")?.addEventListener("scroll", (event) => {
  const list = event.currentTarget;
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight <= 48;
  if (nearBottom) loadNotifications({ append: true });
});
el("notification-refresh")?.addEventListener("click", () => loadNotifications());
el("notification-settings")?.addEventListener("click", () => {
  const preferences = el("notification-preferences"); preferences.classList.toggle("hidden"); updateNotificationChrome();
});
el("notification-compact")?.addEventListener("change", (event) => {
  state.notificationPreferences.compact = event.target.checked;
  localStorage.setItem("verigo_notification_compact", event.target.checked ? "1" : "0");
  applyNotificationPreferences();
});
el("notification-auto-refresh")?.addEventListener("change", (event) => {
  state.notificationPreferences.autoRefresh = event.target.checked;
  localStorage.setItem("verigo_notification_auto_refresh", event.target.checked ? "1" : "0");
  scheduleNotificationPolling();
});
el("notification-reset-preferences")?.addEventListener("click", () => {
  state.notificationPreferences = { compact: false, autoRefresh: true };
  localStorage.removeItem("verigo_notification_compact"); localStorage.removeItem("verigo_notification_auto_refresh");
  applyNotificationPreferences(); scheduleNotificationPolling();
});
el("notification-mark-all")?.addEventListener("click", async () => {
  if (!state.notificationUnread) return;
  const previous = state.notifications.map((notification) => notification.read_at);
  state.notifications.forEach((notification) => { if (!notification.read_at) notification.read_at = new Date().toISOString(); });
  const unread = state.notificationUnread; state.notificationUnread = 0; renderNotifications();
  try { await api("/api/notifications/read", { method: "POST" }); }
  catch (error) {
    state.notifications.forEach((notification, index) => { notification.read_at = previous[index]; });
    state.notificationUnread = unread; renderNotifications();
    setNotificationFeedback(error.message || notificationUiText("无法标记为已读", "Unable to mark as read"), true);
  }
});
document.addEventListener("click", (event) => {
  if (!el("notification-menu").contains(event.target) && !el("notification-button").contains(event.target)) closeNotificationMenu();
  if (!el("account-menu").contains(event.target) && !el("account-button").contains(event.target)) el("account-menu").classList.add("hidden");
  const drawer = el("result-detail-drawer");
  if (drawer?.classList.contains("open") && !drawer.contains(event.target) && !event.target.closest(".result-detail-action")) closeResultDetails();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { closeNotificationMenu(); el("account-menu").classList.add("hidden"); closeResultDetails(); }
});
el("admin-credit-grant-form")?.addEventListener("submit", async (event) => {
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
      ? `已从 ${adjustment.email} 扣减 ${amount} 额度，当前余额 ${adjustment.credits.toLocaleString("zh-CN")}。`
      : `已向 ${adjustment.email} 授予 ${amount} 额度，当前余额 ${adjustment.credits.toLocaleString("zh-CN")}。`;
    el("admin-credit-amount").value = "";
    el("admin-credit-note").value = "";
  } catch (error) {
    result.classList.add("error");
    result.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

el("account-button")?.addEventListener("click", () => {
  if (state.user) el("account-menu").classList.toggle("hidden");
  else el("auth-dialog").showModal();
});
el("logout-button")?.addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.user = null;
  updateAccount();
});
el("delete-account-button")?.addEventListener("click", () => {
  el("account-menu").classList.add("hidden");
  el("delete-account-confirm").checked = false;
  el("delete-account-error").textContent = "";
  el("delete-account-dialog").showModal();
});
el("change-password-button")?.addEventListener("click", () => {
  el("account-menu").classList.add("hidden");
  el("change-password-form").reset();
  el("change-password-error").textContent = "";
  el("change-password-dialog").showModal();
});
function formatApiKeyTime(value) {
  return value ? VerigoI18n.formatDate(value) : VerigoI18n.text("尚未使用");
}

function clearCreatedApiKey() {
  el("api-key-token").value = "";
  el("api-key-created").classList.add("hidden");
  el("copy-api-key").textContent = VerigoI18n.text("复制");
}

async function loadApiKeys() {
  const list = el("api-keys-list");
  list.textContent = VerigoI18n.text("加载中...");
  try {
    const keys = await api("/api/auth/api-keys");
    list.replaceChildren();
    if (!keys.length) {
      const empty = document.createElement("p");
      empty.className = "api-keys-empty";
      empty.textContent = VerigoI18n.text("还没有 API Key。");
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
      detail.textContent = `${key.prefix}... · ${formatApiKeyTime(key.last_used_at)}`;
      info.append(name, detail);
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "account-delete";
      revoke.textContent = VerigoI18n.text("撤销");
      revoke.addEventListener("click", async () => {
        if (!window.confirm(`撤销 API Key “${key.name}”？此操作不能恢复。`)) return;
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
    list.textContent = VerigoI18n.locale === "en" ? `Unable to load API keys: ${error.message}` : `无法加载 API Key：${error.message}`;
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

el("api-nav")?.addEventListener("click", () => {
  if (el("onboarding-dialog")?.open) el("onboarding-dialog").close();
  if (!state.user) {
    el("auth-dialog").showModal();
    setAuthMode("login");
    el("auth-error").textContent = VerigoI18n.text("请先登录后管理 API Key");
    return;
  }
  openApiKeysDialog();
});
el("close-api-keys")?.addEventListener("click", () => el("api-keys-dialog").close());
el("api-keys-dialog")?.addEventListener("close", clearCreatedApiKey);
el("api-keys-refresh")?.addEventListener("click", loadApiKeys);
el("api-key-create-form")?.addEventListener("submit", async (event) => {
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
el("copy-api-key")?.addEventListener("click", async () => {
  const token = el("api-key-token").value;
  if (!token) return;
  try {
    await navigator.clipboard.writeText(token);
    el("copy-api-key").textContent = VerigoI18n.text("已复制");
  } catch (_) {
    el("api-key-token").select();
    document.execCommand("copy");
    el("copy-api-key").textContent = VerigoI18n.text("已复制");
  }
});
el("close-change-password")?.addEventListener("click", () => el("change-password-dialog").close());
el("change-password-form")?.addEventListener("submit", async (event) => {
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
el("close-delete-account")?.addEventListener("click", () => el("delete-account-dialog").close());
el("delete-account-form")?.addEventListener("submit", async (event) => {
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

el("claim-trial-button")?.addEventListener("click", claimTrialCredits);
el("close-email-verification")?.addEventListener("click", () => el("email-verification-dialog").close());
document.querySelectorAll("[data-close-email-verification]").forEach((button) => {
  button.addEventListener("click", () => el("email-verification-dialog").close());
});
el("email-verification-request-form")?.addEventListener("submit", async (event) => {
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
el("email-verification-confirm-form")?.addEventListener("submit", async (event) => {
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

el("bind-email-button")?.addEventListener("click", openBindEmailDialog);
el("close-bind-email")?.addEventListener("click", () => el("bind-email-dialog").close());
document.querySelectorAll("[data-close-bind-email]").forEach((button) => {
  button.addEventListener("click", () => el("bind-email-dialog").close());
});
el("bind-email-request-form")?.addEventListener("submit", async (event) => {
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
el("bind-email-confirm-form")?.addEventListener("submit", async (event) => {
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
el("close-auth")?.addEventListener("click", () => el("auth-dialog").close());
el("auth-form")?.addEventListener("submit", async (event) => {
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
      if (state.pendingRedemption) {
        state.pendingRedemption = false;
        focusRedemptionForm();
      }
    }
    if (state.user && state.view === "workspace") await loadWorkspaceHome();
  } catch (error) {
    el("auth-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

el("open-reset")?.addEventListener("click", () => {
  el("auth-dialog").close();
  el("reset-request-form").classList.remove("hidden");
  el("reset-confirm-form").classList.add("hidden");
  el("reset-error").textContent = "";
  el("reset-dialog").showModal();
});
el("close-reset")?.addEventListener("click", () => el("reset-dialog").close());
document.querySelectorAll("[data-close-reset]").forEach((button) => button.addEventListener("click", () => el("reset-dialog").close()));
el("reset-request-form")?.addEventListener("submit", async (event) => {
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
el("reset-confirm-form")?.addEventListener("submit", async (event) => {
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

el("refresh-jobs")?.addEventListener("click", loadRecentJobs);
el("workspace-history-link")?.addEventListener("click", () => {
  switchView("single");
  window.setTimeout(() => el("recent-block")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
});
el("workspace-api-button")?.addEventListener("click", () => el("api-nav").click());
document.querySelectorAll("#workspace-home [data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));

(async function init() {
  document.title = `${VerigoI18n.text(state.view === "batch" ? "批量验证" : "邮箱验证")} | Verigo`;
  setAuthMode(state.authMode);
  updateCount();
  await loadAccount();
  await loadPublicConfig();
  const requestedAuthMode = new URLSearchParams(window.location.search).get("auth");
  if (!state.user && (requestedAuthMode === "login" || requestedAuthMode === "register")) {
    setAuthMode(requestedAuthMode);
    el("auth-dialog").showModal();
  }
  if (["/workspace", "/history", "/lists", "/dashboard", "/admin/credits", "/wallet", "/app/finder", "/app/history", "/app/billing"].includes(window.location.pathname) || window.location.pathname.startsWith("/lists/")) {
    if (window.location.pathname === "/workspace" && state.user) {
      switchView("workspace");
    } else if ((window.location.pathname === "/lists" || window.location.pathname.startsWith("/lists/"))) {
      window.history.replaceState({}, "", state.user ? "/workspace" : "/");
      if (state.user) switchView("workspace");
      else switchView("single");
    } else if (window.location.pathname === "/workspace" && !state.user) {
      window.history.replaceState({}, "", "/");
      switchView("single");
      state.pendingView = "workspace";
      el("auth-dialog").showModal();
      setAuthMode("login");
      el("auth-error").textContent = "Please sign in to open your workspace";
      return;
    } else if (window.location.pathname === "/history" || window.location.pathname === "/app/history") {
      if (state.user) switchView("history");
      else {
        window.history.replaceState({}, "", "/verify");
        switchView("single");
        setAuthMode("login");
        el("auth-error").textContent = "请先登录后查看历史记录";
        el("auth-dialog").showModal();
      }
    } else if ((window.location.pathname === "/wallet" || window.location.pathname === "/app/billing") && state.user) {
      switchView("wallet");
    } else if (window.location.pathname === "/app/finder" && state.user) {
      switchView("discovery");
    } else if (state.user?.is_admin) {
      switchView(window.location.pathname === "/admin/credits" ? "admin-credits" : "dashboard");
    } else if (state.user) {
      window.location.replace("/");
      return;
    } else {
      el("auth-dialog").showModal();
      setAuthMode("login");
      const requestedPath = window.location.pathname;
      const gateMessage = requestedPath === "/app/finder"
        ? "请先登录后使用企业邮箱查找"
        : (requestedPath === "/wallet" || requestedPath === "/app/billing")
          ? "请先登录后查看账户数据"
          : "请登录有运营监控权限的账户";
      el("auth-error").textContent = gateMessage;
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
