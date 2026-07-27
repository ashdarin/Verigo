const state = {
  run: null, timer: null, pollGeneration: 0,
  contacts: { domain: null, search: "", offset: 0, limit: 50, domainOffset: 0, domainLimit: 50, payload: null },
  results: { runId: null, offset: 0, limit: 50, payload: null },
};
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
  state.contacts.payload = payload;
  const body = byId("saved-contacts-body");
  body.replaceChildren();
  byId("saved-contact-view-title").textContent = state.contacts.domain || "全部已保存联系人";
  byId("saved-contact-view-count").textContent = `${payload.total} 条`;
  byId("saved-previous").disabled = payload.offset === 0;
  byId("saved-contact-count").textContent = `${payload.workspace_total} 条`;
  byId("saved-next").disabled = payload.offset + payload.items.length >= payload.total;
  byId("saved-domain-count").textContent = `${payload.domain_total} 个域名`;
  byId("saved-domain-previous").disabled = payload.domain_offset === 0;
  byId("saved-domain-next").disabled = payload.domain_offset + payload.domains.length >= payload.domain_total;

  const domains = byId("saved-domain-list");
  domains.replaceChildren();
  payload.domains.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `domain-card ${state.contacts.domain === item.domain ? "active" : ""}`;
    const name = document.createElement("strong"); name.textContent = item.domain;
    const meta = document.createElement("span"); meta.textContent = `${item.contact_count} 个已确认联系人`;
    button.append(name, meta);
    button.addEventListener("click", () => {
      state.contacts.domain = item.domain; state.contacts.offset = 0; refreshSavedContacts(); refreshCompany(item.domain).catch((error) => { byId("form-error").textContent = error.message; });
    });
    domains.append(button);
  });
  if (!payload.items.length) {
    const row = document.createElement("tr"); row.className = "empty";
    const cell = document.createElement("td"); cell.colSpan = 4; cell.textContent = "没有匹配的已确认联系人";
    row.append(cell); body.append(row); return;
  }
  payload.items.forEach((item) => {
    const row = document.createElement("tr");
    [item.email, item.domain, `${item.confidence || 0}%`, item.last_verified_at ? new Date(item.last_verified_at).toLocaleString() : "-"].forEach((value) => {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    });
    const tools = document.createElement("td"); tools.className = "contact-tools";
    const favorite = document.createElement("button"); favorite.type = "button";
    favorite.className = `favorite ${item.favorite ? "active" : ""}`;
    favorite.title = item.favorite ? "取消收藏" : "收藏";
    favorite.textContent = item.favorite ? "\u2605" : "\u2606";
    favorite.addEventListener("click", async () => {
      await api("/api/prospecting-beta/saved-contacts", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: item.email, favorite: !item.favorite }) });
      refreshSavedContacts();
    });
    const tag = document.createElement("input"); tag.className = "tag-input"; tag.value = item.tags.join(", "); tag.placeholder = "标签";
    tag.setAttribute("aria-label", `${item.email} 标签`);
    tag.addEventListener("change", async () => {
      const tags = tag.value.split(",").map((value) => value.trim()).filter(Boolean);
      await api("/api/prospecting-beta/saved-contacts", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: item.email, tags }) });
      refreshSavedContacts();
    });
    tools.append(favorite, tag); row.append(tools);
    body.append(row);
  });
}

async function refreshCompany(domain) {
  const company = await api(`/api/prospecting-beta/companies/${encodeURIComponent(domain)}`);
  if (state.contacts.domain !== domain) return;
  const meta = byId("saved-company-meta");
  meta.textContent = `${company.contact_count} 个已确认联系人，${company.favorite_count} 个已收藏，最近验证：${company.last_verified_at ? new Date(company.last_verified_at).toLocaleString() : "-"}`;
  meta.classList.remove("hidden");
}

async function refreshSavedContacts() {
  const query = new URLSearchParams({
    offset: String(state.contacts.offset), limit: String(state.contacts.limit),
    domain_offset: String(state.contacts.domainOffset), domain_limit: String(state.contacts.domainLimit),
  });
  if (state.contacts.domain) query.set("domain", state.contacts.domain);
  if (state.contacts.search) query.set("search", state.contacts.search);
  renderSavedContacts(await api(`/api/prospecting-beta/saved-contacts?${query}`));
}

function renderResults(payload, status) {
  state.results.payload = payload;
  const body = byId("results-body");
  body.replaceChildren();
  byId("result-count").textContent = `${payload.total} 条`;
  byId("result-previous").disabled = payload.offset === 0;
  byId("result-next").disabled = payload.offset + payload.items.length >= payload.total;
  if (!payload.items.length) {
    const row = document.createElement("tr"); row.className = "empty";
    const cell = document.createElement("td"); cell.colSpan = 5;
    cell.textContent = status === "completed" ? "未找到可确认的非 catch-all 企业联系地址" : "正在等待可确认的结果";
    row.append(cell); body.append(row); return;
  }
  payload.items.forEach((item) => {
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

async function refreshRunResults(run) {
  const payload = await api(`/api/prospecting-beta/runs/${run.id}/results?offset=${state.results.offset}&limit=${state.results.limit}`);
  if (state.run?.id === run.id) renderResults(payload, run.status);
}

function renderRun(run) {
  if (state.run?.id !== run.id) {
    state.results = { runId: run.id, offset: 0, limit: 50, payload: null };
  }
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
  const protection = run.protection || { state: "clear" };
  const protectionCopy = protection.state === "waiting"
    ? `已自动暂停，将于 ${new Date(protection.resume_at).toLocaleString()} 后继续。`
    : protection.state === "stopped"
      ? `已自动结束该域名的发现，保护期至 ${new Date(protection.resume_at).toLocaleString()}。`
      : "";
  byId("run-protection").textContent = protectionCopy || protection.message || "";
  byId("run-protection").classList.toggle("hidden", !protectionCopy && !protection.message);
  byId("run-error").textContent = run.error || "";
  refreshRunResults(run).catch((error) => { byId("run-error").textContent = error.message; });
  refreshSavedContacts().catch((error) => { byId("run-error").textContent = error.message; });
}

async function poll() {
  if (!state.run) return;
  const runId = state.run.id;
  const generation = state.pollGeneration;
  try {
    const run = await api(`/api/prospecting-beta/runs/${runId}`);
    // Do not let an in-flight poll restore a task that the user just stopped.
    if (generation !== state.pollGeneration || state.run?.id !== runId) return;
    renderRun(run);
    if (run.status === "queued" || run.status === "running") state.timer = setTimeout(poll, 1500);
  } catch (error) {
    if (generation === state.pollGeneration && state.run?.id === runId) byId("run-error").textContent = error.message;
  }
}

byId("run-form").addEventListener("submit", async (event) => {
  event.preventDefault(); clearTimeout(state.timer); state.pollGeneration += 1; byId("form-error").textContent = "";
  const button = byId("submit"); button.disabled = true;
  try {
    const run = await api("/api/prospecting-beta/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ domain: byId("domain").value, country: byId("country").value, email_pattern: byId("email-pattern").value || null, known_first_name: byId("known-first-name").value || null, known_last_name: byId("known-last-name").value || null, known_email: byId("known-email").value || null }) });
    renderRun(run); poll();
  } catch (error) { byId("form-error").textContent = error.message; }
  finally { button.disabled = false; }
});

byId("stop").addEventListener("click", async () => {
  if (!state.run) return;
  clearTimeout(state.timer); state.pollGeneration += 1;
  byId("stop").disabled = true;
  try { renderRun(await api(`/api/prospecting-beta/runs/${state.run.id}/stop`, { method: "POST" })); }
  catch (error) { byId("run-error").textContent = error.message; }
  finally { byId("stop").disabled = false; }
});

let savedSearchTimer = null;
byId("saved-contact-search").addEventListener("input", (event) => {
  clearTimeout(savedSearchTimer);
  savedSearchTimer = setTimeout(() => {
    state.contacts.search = event.target.value.trim(); state.contacts.offset = 0; state.contacts.domainOffset = 0; refreshSavedContacts();
  }, 250);
});
byId("saved-show-all").addEventListener("click", () => {
  state.contacts.domain = null; state.contacts.offset = 0; byId("saved-company-meta").classList.add("hidden"); refreshSavedContacts();
});
byId("saved-export").addEventListener("click", () => {
  const query = new URLSearchParams();
  if (state.contacts.domain) query.set("domain", state.contacts.domain);
  if (state.contacts.search) query.set("search", state.contacts.search);
  window.location.href = `/api/prospecting-beta/saved-contacts/export?${query}`;
});
byId("saved-previous").addEventListener("click", () => {
  state.contacts.offset = Math.max(0, state.contacts.offset - state.contacts.limit); refreshSavedContacts();
});
byId("saved-next").addEventListener("click", () => {
  const payload = state.contacts.payload;
  if (!payload || state.contacts.offset + payload.items.length >= payload.total) return;
  state.contacts.offset += state.contacts.limit; refreshSavedContacts();
});
byId("saved-domain-previous").addEventListener("click", () => {
  state.contacts.domainOffset = Math.max(0, state.contacts.domainOffset - state.contacts.domainLimit); refreshSavedContacts();
});
byId("saved-domain-next").addEventListener("click", () => {
  const payload = state.contacts.payload;
  if (!payload || payload.domain_offset + payload.domains.length >= payload.domain_total) return;
  state.contacts.domainOffset += state.contacts.domainLimit; refreshSavedContacts();
});
byId("result-previous").addEventListener("click", () => {
  if (!state.run) return;
  state.results.offset = Math.max(0, state.results.offset - state.results.limit); refreshRunResults(state.run);
});
byId("result-next").addEventListener("click", () => {
  const payload = state.results.payload;
  if (!state.run || !payload || payload.offset + payload.items.length >= payload.total) return;
  state.results.offset += state.results.limit; refreshRunResults(state.run);
});

(async () => {
  try { await refreshSavedContacts(); const runs = await api("/api/prospecting-beta/runs"); if (runs.length) { renderRun(runs[0]); if (["queued", "running"].includes(runs[0].status)) poll(); } }
  catch (error) { byId("form-error").textContent = error.message; }
})();
