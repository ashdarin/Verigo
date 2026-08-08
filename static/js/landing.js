const menuToggle = document.getElementById("menu-toggle");
const mainNav = document.getElementById("main-nav");

menuToggle.addEventListener("click", () => {
  const open = mainNav.classList.toggle("open");
  menuToggle.setAttribute("aria-expanded", String(open));
  menuToggle.setAttribute("aria-label", open ? "关闭导航" : "打开导航");
});

mainNav.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => {
  mainNav.classList.remove("open");
  menuToggle.setAttribute("aria-expanded", "false");
}));

const form = document.getElementById("hero-verify-form");
const input = document.getElementById("hero-email");
const submit = document.getElementById("hero-verify-button");
const message = document.getElementById("hero-form-message");
const resultStatus = document.getElementById("hero-result-status");
const resultEmail = document.getElementById("hero-result-email");
const fullResultLink = document.getElementById("full-result-link");
const signalIds = ["signal-syntax", "signal-domain", "signal-mx", "signal-smtp"];

function setSignals(text, positive = false) {
  signalIds.forEach((id) => {
    const element = document.getElementById(id);
    element.textContent = text;
    element.classList.toggle("good-text", positive);
  });
}

function apiError(body, status) {
  const detail = body?.detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg).join("；");
  return detail || `请求失败（${status}）`;
}

async function readJson(response) {
  try { return await response.json(); } catch (_) { return null; }
}

function setResultState(label, className) {
  resultStatus.textContent = label;
  resultStatus.className = `status-pill ${className}`;
}

function setSignal(id, label, state = "pending") {
  const element = document.getElementById(id);
  element.textContent = label;
  element.classList.remove("good-text", "bad-text", "pending-text", "checking-text");
  element.classList.add(`${state}-text`);
}

function renderProgressResult(item) {
  if (!item) return;
  const checks = item.checks || {};
  const label = (value, waiting) => value === true ? "已通过" : value === false ? "未通过" : waiting;
  setSignal("signal-syntax", label(checks.format, "检查中"), checks.format === true ? "good" : checks.format === false ? "bad" : "checking");
  setSignal("signal-domain", label(checks.domain, "检查中"), checks.domain === true ? "good" : checks.domain === false ? "bad" : "checking");
  setSignal("signal-mx", label(checks.mx, "等待 MX 检查"), checks.mx === true ? "good" : checks.mx === false ? "bad" : "pending");
  setSignal("signal-smtp", label(checks.smtp, "等待 SMTP 响应"), checks.smtp === true ? "good" : checks.smtp === false ? "bad" : "pending");
}

function renderCompletedResult(item) {
  renderProgressResult(item);
  const deliverable = item?.deliverable;
  const isDeliverable = deliverable === true || String(deliverable).toLowerCase() === "deliverable";
  const isUndeliverable = deliverable === false || String(deliverable).toLowerCase() === "undeliverable";
  setResultState(isDeliverable ? "可投递" : isUndeliverable ? "无法投递" : "高风险 / 待确认", isDeliverable ? "status-good" : isUndeliverable ? "status-bad" : "status-warn");
  if (isDeliverable || isUndeliverable) setSignal("signal-smtp", isDeliverable ? "可接收" : "不可接收", isDeliverable ? "good" : "bad");
  fullResultLink.classList.remove("hidden");
  message.textContent = "验证完成。你可以在完整结果中查看服务器响应和判断依据。";
}

async function pollJob(jobId, token) {
  for (;;) {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { headers: { "X-Job-Token": token } });
    const job = await readJson(response);
    if (!response.ok) throw new Error(apiError(job, response.status));
    const resultsResponse = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/results?limit=1`, { headers: { "X-Job-Token": token } });
    const results = await readJson(resultsResponse);
    if (!resultsResponse.ok) throw new Error(apiError(results, resultsResponse.status));
    const item = results?.items?.[0] || results?.results?.[0] || null;
    renderProgressResult(item);
    if (job.status === "completed") { renderCompletedResult(item); return; }
    if (job.status === "failed" || job.status === "stopped") throw new Error("本次验证未能完成，请稍后重试。");
    message.textContent = job.status === "queued"
      ? "任务已提交，验证节点即将开始。"
      : `正在验证，已完成 ${job.completed || 0} / ${job.total || 1} 项检查…`;
    await new Promise((resolve) => window.setTimeout(resolve, 900));
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = input.value.trim();
  message.className = "form-message";
  if (!input.checkValidity() || !email) {
    message.className = "form-message error";
    message.textContent = "请输入有效的邮箱地址。";
    input.focus();
    return;
  }

  submit.disabled = true;
  submit.textContent = "正在提交…";
  resultEmail.textContent = email;
  setResultState("验证中", "status-ready");
  const hasDomain = email.includes("@") && email.split("@").pop().includes(".");
  setSignal("signal-syntax", "已通过", "good");
  setSignal("signal-domain", hasDomain ? "已通过" : "待检查", hasDomain ? "good" : "checking");
  setSignal("signal-mx", "等待 MX 检查", "pending");
  setSignal("signal-smtp", "等待 SMTP 响应", "pending");
  fullResultLink.classList.add("hidden");
  message.textContent = "正在检查语法、域名、MX 和 SMTP 信号…";

  try {
    const response = await fetch("/api/verify/single", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    const job = await readJson(response);
    if (!response.ok) throw new Error(apiError(job, response.status));
    const token = job.access_token || "";
    sessionStorage.setItem("verigo_job_id", job.id);
    if (token) sessionStorage.setItem("verigo_job_token", token);
    await pollJob(job.id, token);
  } catch (error) {
    setResultState("未完成", "status-bad");
    setSignals("未检查");
    message.className = "form-message error";
    message.textContent = error.message || "验证失败，请稍后重试。";
  } finally {
    submit.disabled = false;
    submit.textContent = "免费验证邮箱";
  }
});
