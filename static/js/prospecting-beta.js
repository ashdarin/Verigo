const state = { run: null, timer: null };
const byId = (id) => document.getElementById(id);
const labels = { queued: "排队中", running: "验证中", completed: "已完成", failed: "失败", stopped: "已停止" };

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "请求失败");
  return payload;
}

function resultLabel(type) {
  return { verified: "有效", catch_all: "Catch-all", undeliverable: "不可投递", unknown: "待确认" }[type] || type;
}

function categoryLabel(category) {
  return category === "business_entry" ? "业务入口" : "个人格式候选";
}

function renderSavedContacts(payload) {
  const body = byId("saved-contacts-body");
  body.replaceChildren();
  byId("saved-contact-count").textContent = `${payload.total} 条`;
  if (!payload.items.length) {
    const row = document.createElement("tr"); row.className = "empty";
    const cell = document.createElement("td"); cell.colSpan = 4; cell.textContent = "暂未保存有效联系人";
    row.append(cell); body.append(row); return;
  }
  payload.items.forEach((item) => {
    const row = document.createElement("tr");
    [item.email, item.domain, item.pattern, new Date(item.saved_at).toLocaleString()].forEach((value) => {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    });
    body.append(row);
  });
}

async function refreshSavedContacts() {
  renderSavedContacts(await api("/api/prospecting-beta/saved-contacts"));
}

function renderResults(run) {
  const body = byId("results-body");
  body.replaceChildren();
  const visible = run.results.filter((item) => item.result_type === "verified" || item.result_type === "catch_all");
  byId("result-count").textContent = `${visible.length} 条`;
  if (!visible.length) {
    const row = document.createElement("tr"); row.className = "empty";
    const cell = document.createElement("td"); cell.colSpan = 5;
    cell.textContent = run.status === "completed" ? "未找到可确认的非 catch-all 企业联系地址" : "正在等待可确认的结果";
    row.append(cell); body.append(row); return;
  }
  visible.forEach((item) => {
    const row = document.createElement("tr");
    const verification = item.verification || {};
    const values = [
      item.email,
      categoryLabel(item.category),
      item.pattern,
      resultLabel(item.result_type),
      verification.smtp_result || verification.message || "-",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 3) { const pill = document.createElement("span"); pill.className = `pill ${item.result_type}`; pill.textContent = value; cell.append(pill); }
      else { cell.textContent = value; if (index === 4) cell.className = "detail"; }
      row.append(cell);
    });
    body.append(row);
  });
}

function renderRun(run) {
  state.run = run;
  byId("run-panel").classList.remove("hidden"); byId("results-panel").classList.remove("hidden");
  byId("run-domain").textContent = `${run.domain} (${run.country})`;
  const status = byId("run-status"); status.textContent = labels[run.status] || run.status; status.className = `status ${run.status}`;
  const active = run.status === "queued" || run.status === "running";
  byId("stop").classList.toggle("hidden", !active);
  byId("progress-copy").textContent = `${run.completed} / ${run.total} 已处理`;
  byId("progress-value").textContent = `${run.progress}%`; byId("progress-bar").style.width = `${run.progress}%`;
  byId("metric-total").textContent = run.total; byId("metric-completed").textContent = run.completed;
  byId("metric-verified").textContent = run.summary.verified || 0; byId("metric-catchall").textContent = run.summary.catch_all || 0;
  const selectedPattern = run.requested_pattern ? `已优先使用你提供的规则：${run.requested_pattern}。` : "未提供邮箱规则，已按国家姓名库生成候选。";
  const learnedPattern = run.profile_patterns.length ? ` 已学习规则：${run.profile_patterns.join("、")}。` : "";
  byId("profile-copy").textContent = `${selectedPattern}${learnedPattern}`;
  byId("run-error").textContent = run.error || "";
  renderResults(run);
  refreshSavedContacts().catch((error) => { byId("run-error").textContent = error.message; });
}

async function poll() {
  if (!state.run) return;
  try {
    const run = await api(`/api/prospecting-beta/runs/${state.run.id}`); renderRun(run);
    if (run.status === "queued" || run.status === "running") state.timer = setTimeout(poll, 1500);
  } catch (error) { byId("run-error").textContent = error.message; }
}

byId("run-form").addEventListener("submit", async (event) => {
  event.preventDefault(); clearTimeout(state.timer); byId("form-error").textContent = "";
  const button = byId("submit"); button.disabled = true;
  try {
    const run = await api("/api/prospecting-beta/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ domain: byId("domain").value, country: byId("country").value, email_pattern: byId("email-pattern").value || null, known_first_name: byId("known-first-name").value || null, known_last_name: byId("known-last-name").value || null, known_email: byId("known-email").value || null }) });
    renderRun(run); poll();
  } catch (error) { byId("form-error").textContent = error.message; }
  finally { button.disabled = false; }
});

byId("stop").addEventListener("click", async () => {
  if (!state.run) return;
  byId("stop").disabled = true;
  try { renderRun(await api(`/api/prospecting-beta/runs/${state.run.id}/stop`, { method: "POST" })); }
  catch (error) { byId("run-error").textContent = error.message; }
  finally { byId("stop").disabled = false; }
});

(async () => {
  try { await refreshSavedContacts(); const runs = await api("/api/prospecting-beta/runs"); if (runs.length) { renderRun(runs[0]); if (["queued", "running"].includes(runs[0].status)) poll(); } }
  catch (error) { byId("form-error").textContent = error.message; }
})();
