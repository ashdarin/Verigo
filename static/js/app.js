const state = {
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
};

const pageSize = 50;

const el = (id) => document.getElementById(id);
const input = el("email-input");
const count = el("email-count");
const startButton = el("start-button");
const errorBox = el("form-error");
const statusLabels = { queued: "排队中", running: "验证中", completed: "已完成", failed: "失败" };
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
  return state.mode === "file" ? state.fileEmails : splitEmails(input.value);
}

function updateCount() {
  const total = currentEmails().length;
  count.textContent = total.toLocaleString();
  if (state.mode === "paste" && total === 1) {
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
  const discovery = view === "discovery";
  if (discovery && !state.user) {
    el("auth-dialog").showModal();
    setAuthMode("login");
    el("auth-error").textContent = "请先登录后使用工作邮箱查找";
    return;
  }
  el("verify-workspace").classList.toggle("hidden", discovery);
  el("discovery-workspace").classList.toggle("hidden", !discovery);
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
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

input.addEventListener("input", updateCount);

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
    errorBox.textContent = "请至少输入一个邮箱地址";
    return;
  }
  startButton.disabled = true;
  startButton.textContent = "正在提交…";
  try {
    state.guestToken = null;
    const workerCount = Number(document.querySelector('input[name="speed"]:checked').value);
    const isFreeSingle = state.mode === "paste" && emails.length === 1;
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
  const [modeLabel, modeClass] = modeLabels[job.worker_count] || ["自定义模式", "mode-standard"];
  mode.textContent = modeLabel;
  mode.className = `mode-badge ${modeClass}`;
  el("progress-percent").textContent = `${job.progress}%`;
  el("progress-bar").style.width = `${job.progress}%`;
  el("progress-copy").textContent = job.error
    || (job.status === "queued" && job.queue_position ? `排队中，前方还有 ${job.queue_position - 1} 个任务` : `${job.completed} / ${job.total} 已处理`);
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
    if (job.status === "completed") {
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
  el("discovery-progress-percent").textContent = `${job.progress}%`;
  el("discovery-progress-bar").style.width = `${job.progress}%`;
  el("discovery-progress-copy").textContent = job.status === "queued" && job.queue_position
    ? `排队中，前方还有 ${job.queue_position - 1} 个任务`
    : `${job.completed} / ${job.total} 已处理`;
}

function updateDiscoveryVerdict(job) {
  const verdict = el("discovery-verdict");
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
    if (job.status !== "completed" && job.status !== "failed") setTimeout(pollDiscovery, 1200);
  } catch (error) {
    el("discovery-error").textContent = error.message;
  }
}

el("discovery-start").addEventListener("click", async () => {
  const error = el("discovery-error");
  error.textContent = "";
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
    verifyButton.textContent = `验证候选邮箱 · ${state.discovery.candidates.length} 额度`;
    el("discovery-title").textContent = `${state.discovery.candidates.length} 个候选邮箱`;
    el("discovery-status").textContent = "已找到";
    el("discovery-status").className = "status status-completed";
    el("discovery-progress-percent").textContent = "0%";
    el("discovery-progress-bar").style.width = "0%";
    el("discovery-progress-copy").textContent = "等待验证";
    el("discovery-verdict").className = "discovery-verdict";
    el("discovery-verdict").textContent = `已生成 ${state.discovery.candidates.length} 个候选地址`;
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
  button.disabled = true;
  let submitted = false;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        emails: state.discovery.candidates,
        worker_count: 4,
        stop_on_deliverable: true,
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
    ? `${state.user.credits || 0} 额度${trialCredits ? ` · ${trialCredits} 体验额度` : ""}`
    : "";
  el("account-credits").title = state.user?.trial_credit_expires_at
    ? `体验额度有效至 ${new Date(state.user.trial_credit_expires_at).toLocaleString("zh-CN")}`
    : "";
  el("bind-email-button").classList.toggle("hidden", !state.user?.needs_email_binding);
  el("verify-email-button").classList.toggle(
    "hidden", !state.user || state.user.needs_email_binding || state.user.email_verified,
  );
  el("recent-block").classList.toggle("hidden", !state.user);
  el("account-menu").classList.add("hidden");
  if (state.user) loadRecentJobs();
}

async function loadAccount() {
  try { state.user = await api("/api/auth/me"); } catch (_) { state.user = null; }
  updateAccount();
}

el("account-button").addEventListener("click", () => {
  if (state.user) el("account-menu").classList.toggle("hidden");
  else el("auth-dialog").showModal();
});
el("logout-button").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.user = null;
  updateAccount();
});
el("verify-email-button").addEventListener("click", async () => {
  try {
    await api("/api/auth/email-verification/request", { method: "POST" });
    const code = window.prompt("请输入邮件中的六位验证码");
    if (!code) return;
    state.user = await api("/api/auth/email-verification/confirm", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
    });
    updateAccount();
  } catch (error) { errorBox.textContent = error.message; }
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
  document.querySelectorAll("[data-auth-mode]").forEach((button) => button.classList.toggle("active", button.dataset.authMode === mode));
  el("auth-error").textContent = "";
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
      }),
    });
    el("auth-dialog").close();
    el("auth-form").reset();
    updateAccount();
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
