const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const resultDetailFixtures = require("./fixtures/result_detail_states.json");
const BASE_URL = process.env.VERIGO_UI_BASE || "http://127.0.0.1:8000/verify";
const BASE_ORIGIN = new URL(BASE_URL).origin;

async function checkViewport(browser, name, width, height) {
  const page = await browser.newPage({ viewport: { width, height } });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(JSON.stringify({ text: message.text(), location: message.location() }));
    }
  });
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

  const result = await page.evaluate(() => {
    const visible = (selector) => {
      const node = document.querySelector(selector);
      if (!node) return false;
      const rect = node.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    return {
      title: document.title,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      startVisible: visible("#start-button"),
      progressVisible: visible(".progress-section"),
      tableVisible: visible(".table-wrap"),
      apiDocsHref: document.querySelector('.site-footer a[href="/api-docs"]')?.getAttribute("href"),
      apiNavVisible: visible("#api-nav"),
    };
  });

  if (!result.title.includes("Verigo")) throw new Error(`${name}: unexpected title`);
  if (result.overflow) throw new Error(`${name}: page has horizontal overflow`);
  if (!result.startVisible || !result.progressVisible || !result.tableVisible || !result.apiNavVisible || result.apiDocsHref !== "/api-docs") {
    throw new Error(`${name}: a primary UI region is hidden`);
  }
  await page.click("#api-nav");
  if (!(await page.locator("#auth-dialog").evaluate((node) => node.open))) {
    throw new Error(`${name}: API navigation must request sign-in when logged out`);
  }
  if (errors.length) throw new Error(`${name}: console errors: ${errors.join(" | ")}`);
  await page.close();
  return { name, ...result };
}

async function checkRiskPresentation(browser) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  const result = await page.evaluate(() => {
    const disposable = riskSignalPresentation.find((item) => item.key === "disposable_provider");
    const pending = riskSignalStatus(disposable, { detected: false });
    const detected = riskSignalStatus(disposable, { detected: true });
    const detail = riskSignalDetail(disposable, {
      detected: true, detail: "internal detail must not render",
    });
    return { pending, detected, detail };
  });
  if (result.pending.value !== "\u6682\u65e0\u6cd5\u786e\u8ba4"
    || result.detected.value !== "\u4e00\u6b21\u6027\u90ae\u7bb1\u670d\u52a1"
    || result.detail.includes("internal detail")) {
    throw new Error(`risk presentation: unexpected rendering ${JSON.stringify(result)}`);
  }
  await page.close();
  return { riskPresentation: true };
}

async function checkResultDetailsViewport(browser, name, viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(JSON.stringify({ text: message.text(), location: message.location() }));
    }
  });
  await page.addInitScript(() => {
    sessionStorage.setItem("verigo_job_id", "ui-result-details");
    sessionStorage.setItem("verigo_job_token", "ui-result-token");
  });
  await page.route("**/api/auth/me", (route) => route.fulfill({
    contentType: "application/json", body: "null",
  }));
  await page.route("**/api/public/config", (route) => route.fulfill({
    contentType: "application/json", body: JSON.stringify({}),
  }));
  await page.route("**/api/jobs/ui-result-details", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      id: "ui-result-details", status: "completed", worker_count: 1,
      completed: resultDetailFixtures.length, total: resultDetailFixtures.length, progress: 100,
      summary: { total: resultDetailFixtures.length, deliverable: 1, undeliverable: 2, unknown: 2 },
      created_at: "2026-08-16T00:00:00Z", started_at: "2026-08-16T00:00:01Z",
      finished_at: "2026-08-16T00:01:00Z", retry_at: "2000-01-01T00:00:00Z",
      download_url: null, error: null, queue_position: null, qq_slow: false,
    }),
  }));
  await page.route("**/api/jobs/ui-result-details/results?**", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      total: resultDetailFixtures.length, available: resultDetailFixtures.length,
      offset: 0, limit: 50, items: resultDetailFixtures,
    }),
  }));
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForFunction((count) => document.querySelectorAll("#results-body .result-email-row").length === count, resultDetailFixtures.length);

  const initial = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    progress: document.querySelector("#progress-copy")?.textContent || "",
    actionSizes: [...document.querySelectorAll("#results-body .result-detail-action")].map((node) => {
      const rect = node.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    }),
    emailStyle: (() => {
      const style = getComputedStyle(document.querySelector(".result-email-value"));
      return { whiteSpace: style.whiteSpace, overflowWrap: style.overflowWrap };
    })(),
  }));
  if (initial.overflow) throw new Error(`${name} result details: page has horizontal overflow`);
  if (initial.progress.includes("0 秒") || initial.progress.includes("后再次复核")) {
    throw new Error(`${name} result details: expired retry countdown is still visible: ${initial.progress}`);
  }
  if (initial.actionSizes.some((size) => size.width < 40 || size.height < 40)) {
    throw new Error(`${name} result details: detail action is smaller than 40px`);
  }
  if (initial.emailStyle.whiteSpace !== "normal" || initial.emailStyle.overflowWrap !== "anywhere") {
    throw new Error(`${name} result details: long email wrapping is not enabled`);
  }

  const expectedStatus = {
    deliverable: "可投递",
    undeliverable: "不可投递",
    greylist_452: "无法确认",
    mailbox_full: "不可投递",
    retry_completed: "无法确认",
  };
  const screenshotDir = process.env.VERIGO_RESULT_DETAIL_SCREENSHOT_DIR;
  if (screenshotDir) fs.mkdirSync(screenshotDir, { recursive: true });

  for (let index = 0; index < resultDetailFixtures.length; index += 1) {
    const fixture = resultDetailFixtures[index];
    await page.locator("#results-body .result-detail-action").nth(index).click();
    await page.waitForFunction(() => document.querySelector("#result-detail-drawer")?.classList.contains("open"));
    await page.waitForFunction(() => {
      const node = document.querySelector("#result-detail-drawer");
      if (!node) return false;
      const rect = node.getBoundingClientRect();
      return rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight;
    });
    const drawer = page.locator("#result-detail-drawer");
    const rendered = await drawer.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return {
        status: node.querySelector("#result-detail-status")?.textContent,
        headings: [...node.querySelectorAll(".detail-section-heading h3")].map((item) => item.textContent),
        conclusion: [...node.querySelectorAll(".detail-conclusion-field")].map((item) => item.textContent),
        technicalCount: node.querySelectorAll(".technical-details").length,
        technicalOpen: node.querySelector(".technical-details")?.open,
        withinViewport: rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight,
      };
    });
    if (rendered.status !== expectedStatus[fixture.case]) {
      throw new Error(`${name} ${fixture.case}: unexpected status ${rendered.status}`);
    }
    if (rendered.headings.join("|") !== "验证结论|可投递性检查|地址特征与投递风险") {
      throw new Error(`${name} ${fixture.case}: result sections are in the wrong order ${rendered.headings.join("|")}`);
    }
    if (rendered.technicalCount !== 1 || rendered.technicalOpen || !rendered.withinViewport) {
      throw new Error(`${name} ${fixture.case}: technical details or drawer geometry is invalid ${JSON.stringify(rendered)}`);
    }
    if (fixture.case === "mailbox_full" && !rendered.conclusion.some((value) => value.includes("收件箱已满，暂时无法接收邮件"))) {
      throw new Error(`${name} mailbox full: the user action is missing`);
    }
    if (screenshotDir && index === 0) {
      await page.screenshot({ path: path.join(screenshotDir, `result-details-${name}.png`), fullPage: false });
    }

    await drawer.locator(".technical-details summary").click();
    const technicalFields = await drawer.locator(".technical-detail").evaluateAll((rows) => rows.map((row) => ({
      label: row.querySelector("span")?.textContent,
      value: row.querySelector(".technical-value")?.textContent,
    })));
    const serverResponse = technicalFields.find((field) => field.label === "服务器响应")?.value;
    if (serverResponse !== fixture.smtp_result) {
      throw new Error(`${name} ${fixture.case}: SMTP response changed: ${JSON.stringify(serverResponse)}`);
    }

    if (index === 0 && viewport.width > 600) {
      await page.mouse.click(20, 100);
      await page.waitForFunction(() => !document.querySelector("#result-detail-drawer")?.classList.contains("open"));
    } else {
      await page.click("#close-result-detail");
    }
  }
  if (errors.length) throw new Error(`${name} result details: console errors: ${errors.join(" | ")}`);
  await page.close();
  return { name, cases: resultDetailFixtures.length, noOverflow: true, smtpPassthrough: true };
}

async function checkResultDetails(browser) {
  return {
    resultDetails: [
      await checkResultDetailsViewport(browser, "desktop", { width: 1440, height: 900 }),
      await checkResultDetailsViewport(browser, "mobile", { width: 390, height: 844 }),
    ],
  };
}

async function checkAccountAndImport(browser) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.setExtraHTTPHeaders({ "x-forwarded-for": `198.51.100.${Math.floor(Math.random() * 200) + 1}` });
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.click("#account-button");
  await page.click('[data-auth-mode="register"]');
  const email = `ui_${Date.now()}@example.com`;
  await page.fill("#auth-email", email);
  await page.fill("#auth-password", "browser-smoke-2026");
  await page.click("#auth-submit");
  await page.waitForFunction((value) => (
    document.querySelector("#account-button")?.textContent === value
    || document.querySelector("#auth-error")?.textContent.trim()
  ), email);
  const authError = (await page.locator("#auth-error").textContent()).trim();
  if (authError) throw new Error(`account registration failed: ${authError}`);
  if (await page.locator("#onboarding-dialog").evaluate((node) => node.open)) {
    await page.locator("#onboarding-dialog [data-close-onboarding]").first().click();
  }
  if (await page.locator("#recent-block").evaluate((node) => node.classList.contains("hidden"))) {
    throw new Error("account: recent jobs should be visible after login");
  }
  if (await page.locator("#claim-trial-button").evaluate((node) => node.classList.contains("hidden"))) {
    throw new Error("account: the trial-credit action should be prominent for unverified users");
  }
  await page.click("#claim-trial-button");
  if (!(await page.locator("#email-verification-dialog").evaluate((node) => node.open))) {
    throw new Error("account: email verification should use the in-app dialog");
  }
  await page.click("#close-email-verification");
  await page.click("#api-nav");
  if (!(await page.locator("#api-keys-dialog").evaluate((node) => node.open))) {
    throw new Error("api keys: management must be available from the main navigation");
  }
  await page.fill("#api-key-name", "browser smoke");
  await page.click("#api-key-create-submit");
  await page.waitForFunction(() => document.querySelector("#api-key-token")?.value.startsWith("vg_live_"));
  await page.click("#close-api-keys");
  await page.click("#account-button");
  await page.click("#change-password-button");
  if (!(await page.locator("#change-password-dialog").evaluate((node) => node.open))) {
    throw new Error("account: password changes should use the in-app dialog");
  }
  await page.fill("#current-password", "browser-smoke-2026");
  await page.fill("#new-password", "browser-smoke-updated-2026");
  await page.click("#change-password-form button[type=submit]");
  await page.waitForFunction(() => !document.querySelector("#change-password-dialog")?.open);
  await page.click("#account-button");
  await page.click("#delete-account-button");
  if (!(await page.locator("#delete-account-dialog").evaluate((node) => node.open))) {
    throw new Error("account: deletion must require an in-app confirmation dialog");
  }
  await page.click("#close-delete-account");

  await page.click('[data-view="batch"]');
  await page.click('[data-mode="file"]');
  await page.setInputFiles("#file-input", {
    name: "contacts.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("name,email\nA,first@example.com\nB,second@example.cn"),
  });
  await page.waitForFunction(() => document.querySelector("#email-count")?.textContent === "2");
  if (!(await page.textContent("#start-button")).includes("2 额度")) {
    throw new Error("pricing: imported addresses must use paid verification");
  }
  await page.click('[data-mode="paste"]');
  await page.fill("#email-input", "single@example.com");
  if (!(await page.textContent("#start-button")).includes("1 额度")) {
    throw new Error("pricing: a batch entry must be paid even when it has one address");
  }
  await page.fill("#email-input", "one@example.com\ntwo@example.com");
  if (!(await page.textContent("#start-button")).includes("2 额度")) {
    throw new Error("pricing: multiple manually entered addresses must be paid");
  }
  await page.fill("#email-input", "demo@qq.com");
  if (await page.locator("#qq-rate-notice").evaluate((node) => node.classList.contains("hidden"))) {
    throw new Error("qq: low-concurrency notice should appear before submission");
  }
  await page.click('[data-view="single"]');
  await page.fill("#single-email-input", "single@example.com");
  if ((await page.textContent("#start-button")) !== "免费验证") {
    throw new Error("pricing: the single-verification view should remain free");
  }
  await page.click('[data-view="discovery"]');
  await page.fill("#discovery-first-name", "Ming");
  await page.fill("#discovery-last-name", "Wang");
  await page.fill("#discovery-domain", "example.com");
  await page.click("#discovery-start");
  await page.waitForFunction(() => document.querySelectorAll("#discovery-candidates span").length > 0);
  if (await page.isDisabled("#discovery-verify")) {
    throw new Error("discovery: candidate verification should be available after free lookup");
  }
  if (!(await page.textContent("#discovery-verify")).includes("免费验证候选邮箱")) {
    throw new Error("discovery: candidate verification must be visibly free");
  }
  if (!(await page.locator("#stop-job-button").count()) || !(await page.locator("#discovery-stop-button").count())) {
    throw new Error("verification: both workspaces need a stop control");
  }
  if (await page.locator("#discovery-stop-on-match").count()) {
    throw new Error("discovery: stop-after-match must be the fixed default, not a user option");
  }
  await page.close();
  return { account: true, importCount: 2, discovery: true };
}

async function checkMobileTrialAction(browser) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.setExtraHTTPHeaders({ "x-forwarded-for": `198.51.100.${Math.floor(Math.random() * 200) + 1}` });
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.click("#account-button");
  await page.click('[data-auth-mode="register"]');
  await page.fill("#auth-email", `mobile_${Date.now()}@example.com`);
  await page.fill("#auth-password", "browser-smoke-2026");
  await page.click("#auth-submit");
  await page.waitForFunction(() => !document.querySelector("#claim-trial-button")?.classList.contains("hidden"));
  if (await page.locator("#onboarding-dialog").evaluate((node) => node.open)) {
    await page.locator("#onboarding-dialog [data-close-onboarding]").first().click();
  }
  await page.click("#api-nav");
  if (!(await page.locator("#api-keys-dialog").evaluate((node) => node.open))) {
    throw new Error("mobile API keys: management dialog should open");
  }
  if (await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)) {
    throw new Error("mobile API keys: page has horizontal overflow");
  }
  await page.close();
  return { mobileTrialAction: true };
}

async function checkEnglishLocale(browser) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.route("**/api/auth/me", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      id: "locale-user", email: "locale@example.com", email_verified: true,
      credits: 10, paid_credits: 10, trial_credits: 0, trial_credit_expires_at: null,
      needs_email_binding: false, is_admin: false,
    }),
  }));
  await page.route(/\/api\/notifications(?:\?.*)?$/, (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      unread_count: 1,
      items: [{
        id: "locale-credit", kind: "credit_grant", title: "额度已到账",
        body: "管理员已向你的账户增加 1,000 额度。", created_at: "2026-07-24T12:00:00Z", read_at: null,
      }],
    }),
  }));
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => document.querySelector("#locale-toggle").click());
  await page.fill("#single-email-input", "locale-check@yahoo.com");
  await page.click("#start-button");
  await page.waitForFunction(() => document.querySelectorAll("#results-body td").length >= 3);
  const result = await page.evaluate(() => ({
    lang: document.documentElement.lang,
    code: document.querySelector("#locale-code")?.textContent,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    chinese: (() => { const root = document.querySelector("#verify-workspace"); const values = []; if (!root) return values; const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); let node; while ((node = walker.nextNode())) { const value = node.nodeValue.trim(); if (value && /[\u4e00-\u9fff]/.test(value) && node.parentElement?.getClientRects().length) values.push(value); } return values; })(),
    values: [...document.querySelectorAll("#results-body td")].map((node) => node.textContent.trim()),
    notification: document.querySelector("#notification-list")?.textContent.trim(),
    fallbackDetail: VerigoI18n.resultValue("服务器暂未确认 450"),
    accountLabel: getComputedStyle(document.querySelector("#account-button"), "::after").content,
    localeIcon: document.querySelector("#locale-toggle i")?.className,
  }));
  const workbenchChinese = result.chinese.filter((value) => /邮箱|验证|可投递|不可投递|无法确认|结果|状态/.test(value));
  if (result.lang !== "en" || result.code !== "EN" || result.overflow || workbenchChinese.length) {
    throw new Error(`english locale: unexpected rendering ${JSON.stringify(result)}`);
  }
  if (!result.values.some((value) => ["Unsupported validation", "Stopped", "Undeliverable", "Unable to confirm"].includes(value))) {
    throw new Error(`english locale: result detail was not localized ${JSON.stringify(result.values)}`);
  }
  if (!result.notification.includes("Credits added") || !result.notification.includes("An administrator added 1,000 credits to your account.") || result.fallbackDetail !== "Mail-server response (450)" || result.accountLabel !== '"Account"' || !result.localeIcon.includes("fa-solid")) {
    throw new Error(`english locale: notification or fallback was not localized ${JSON.stringify(result)}`);
  }
  await page.close();
  return { englishLocale: true };
}

async function checkEnglishDiscoveryAndDocs(browser) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.route("**/api/auth/me", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      id: "coverage-user", email: "coverage@example.com", email_verified: true,
      credits: 10, paid_credits: 10, trial_credits: 0, trial_credit_expires_at: null,
      needs_email_binding: false, is_admin: false,
    }),
  }));
  await page.route("**/api/jobs?offset=0&limit=8", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ total: 0, offset: 0, limit: 8, items: [] }) }));
  await page.route(/\/api\/notifications(?:\?.*)?$/, (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [], unread_count: 0 }) }));
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => document.querySelector("#locale-toggle").click());
  await page.click('[data-view="batch"]');
  await page.fill("#email-input", "coverage@qq.com");
  await page.click('[data-view="discovery"]');
  const main = await page.evaluate(() => ({
    chinese: (() => { const root = document.querySelector("#discovery-workspace"); const values = []; if (!root) return values; const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); let node; while ((node = walker.nextNode())) { const value = node.nodeValue.trim(); if (value && /[\u4e00-\u9fff]/.test(value) && node.parentElement?.getClientRects().length) values.push(value); } return values; })(),
    title: document.querySelector("#discovery-workspace h2")?.textContent,
    search: document.querySelector("#discovery-start")?.textContent,
    qqNotice: document.querySelector("#qq-rate-notice")?.textContent,
    nodeMessage: VerigoI18n.text("腾讯 QQ 验证节点正在启动，请稍候"),
  }));
  if (main.chinese.length || main.title !== "Find a work email by name" || main.search !== "Find emails" || !main.qqNotice.includes("automatic backoff") || main.nodeMessage !== "Tencent QQ verification node is starting. Please wait.") {
    throw new Error(`english discovery: untranslated content ${JSON.stringify(main)}`);
  }
  await page.close();

  const docs = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await docs.goto(`${BASE_ORIGIN}/api-docs`, { waitUntil: "domcontentloaded" });
  await docs.click("#docs-locale-toggle");
  const documentation = await docs.evaluate(() => ({
    lang: document.documentElement.lang,
    code: document.querySelector("#docs-locale-code")?.textContent,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    chinese: (() => { const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); const values = []; let node; while ((node = walker.nextNode())) { const value = node.nodeValue.trim(); if (value && /[\u4e00-\u9fff]/.test(value) && node.parentElement?.getClientRects().length) values.push(value); } return values; })(),
  }));
  if (documentation.lang !== "en" || documentation.code !== "EN" || documentation.overflow || documentation.chinese.length) {
    throw new Error(`english API documentation: untranslated content ${JSON.stringify(documentation)}`);
  }
  await docs.click("#docs-locale-toggle");
  if ((await docs.locator("html").getAttribute("lang")) !== "zh-CN") {
    throw new Error("API documentation: locale toggle must restore Chinese");
  }
  await docs.close();
  return { englishDiscoveryAndDocs: true };
}

async function checkEnglishDesktopHeadingAndApiKeys(browser) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.route("**/api/auth/me", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      id: "desktop-locale-user", email: "desktop@example.com", email_verified: true,
      credits: 10, paid_credits: 10, trial_credits: 0, trial_credit_expires_at: null,
      needs_email_binding: false, is_admin: false,
    }),
  }));
  await page.route("**/api/jobs?offset=0&limit=8", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ total: 0, offset: 0, limit: 8, items: [] }) }));
  await page.route(/\/api\/notifications(?:\?.*)?$/, (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [], unread_count: 0 }) }));
  await page.route("**/api/auth/api-keys", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{ id: "desktop-key", name: "production", prefix: "vg_live_12345678", last_used_at: null }]),
  }));
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.click("#locale-toggle");
  const measureHeading = () => page.evaluate(() => {
    const heading = document.querySelector("#verify-heading").getBoundingClientRect();
    const count = document.querySelector("#email-count").getBoundingClientRect();
    return { text: document.querySelector("#verify-heading").textContent, overlaps: heading.right > count.left, headingRight: heading.right, countLeft: count.left };
  });
  const single = await measureHeading();
  await page.click('[data-view="batch"]');
  const batch = await measureHeading();
  await page.click("#api-nav");
  await page.waitForFunction(() => document.querySelectorAll(".api-key-row").length === 1);
  const apiKeys = await page.evaluate(() => ({
    placeholder: document.querySelector("#api-key-name").getAttribute("placeholder"),
    detail: document.querySelector(".api-key-row small")?.textContent,
    chinese: (() => { const walker = document.createTreeWalker(document.querySelector("#api-keys-dialog"), NodeFilter.SHOW_TEXT); const values = []; let node; while ((node = walker.nextNode())) { const value = node.nodeValue.trim(); if (value && /[\u4e00-\u9fff]/.test(value) && node.parentElement?.getClientRects().length) values.push(value); } return values; })(),
  }));
  if (single.text !== "Verify an email" || batch.text !== "Verify emails in bulk" || single.overlaps || batch.overlaps || apiKeys.placeholder !== "e.g. Production" || !apiKeys.detail?.includes("Not used yet") || apiKeys.chinese.length) {
    throw new Error(`english desktop heading/API keys: invalid rendering ${JSON.stringify({ single, batch, apiKeys })}`);
  }
  await page.close();
  return { englishDesktopHeadingAndApiKeys: true };
}

async function checkDashboard(browser) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.route("**/api/auth/me", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      id: "admin", email: "admin@example.com", email_verified: true,
      credits: 0, paid_credits: 0, trial_credits: 0, trial_credit_expires_at: null,
      needs_email_binding: false, is_admin: true,
    }),
  }));
  await page.route("**/api/admin/metrics", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      updated_at: new Date().toISOString(),
      today: {
        page_views: 42, unique_visitors: 17, new_users: 3, new_jobs: 5, credits_consumed: 12, revenue_fen: 2990, paid_orders: 2,
        sessions: 12, suspected_bots: 2, engaged_sessions: 6, bounce_rate: 25, bot_rate: 16.7,
        average_engagement_seconds: 94, free_submissions: 4, batch_submissions: 2, verified_users: 2,
        job_completion_rate: 80, average_job_seconds: 31, deliverable_rate: 70, results_processed: 20,
      },
      provider_quality: {
        total: 1000, deliverable: 850, undeliverable: 80, unknown: 70, reviewed: 50,
        risk_flags: { disposable: 12, mailbox_full: 8, role_address: 25, do_not_reply: 6 },
        providers: [
          { provider: "gmail", processed: 600, deliverable_rate: 86.7, unconfirmed_rate: 6.0, review_completion_rate: 80.0, latency_sample: 580, p50_seconds: 8, p95_seconds: 30, review_latency_sample: 20, review_p50_seconds: 900, review_p95_seconds: 3600 },
          { provider: "microsoft", processed: 250, deliverable_rate: 84.0, unconfirmed_rate: 8.0, review_completion_rate: null, latency_sample: 250, p50_seconds: 10, p95_seconds: 45, review_latency_sample: 0, review_p50_seconds: 0, review_p95_seconds: 0 },
          { provider: "qq", processed: 100, deliverable_rate: 81.0, unconfirmed_rate: 11.0, review_completion_rate: 60.0, latency_sample: 90, p50_seconds: 15, p95_seconds: 70, review_latency_sample: 10, review_p50_seconds: 1800, review_p95_seconds: 4200 },
          { provider: "other", processed: 50, deliverable_rate: 82.0, unconfirmed_rate: 10.0, review_completion_rate: null, latency_sample: 50, p50_seconds: 18, p95_seconds: 90, review_latency_sample: 0, review_p50_seconds: 0, review_p95_seconds: 0 },
        ],
        baseline: {
          window_days: 7, minimum_daily_sample: 50,
          providers: [
            { provider: "gmail", usable_days: 7, days: 7, ready: true, baseline_unconfirmed_rate: 5.0, baseline_p95_seconds: 28, suggested_unconfirmed_percent: 7.5, suggested_p95_seconds: 58 },
            { provider: "microsoft", usable_days: 4, days: 7, ready: false, baseline_unconfirmed_rate: 7.0, baseline_p95_seconds: 40, suggested_unconfirmed_percent: 10.5, suggested_p95_seconds: 70 },
          ],
        },
      },
      totals: { page_views: 200, unique_visitors: 80, users: 31, verified_users: 20, jobs: 50, revenue_fen: 5990, paid_orders: 4 },
      jobs: { queued: 1, running: 2, completed: 45, failed: 2 },
      daily: Array.from({ length: 14 }, (_, index) => ({
        day: `2026-07-${String(index + 1).padStart(2, "0")}`, page_views: index + 1,
        unique_visitors: index + 1, engaged_sessions: Math.max(0, index - 1),
      })),
    }),
  }));
  await page.route("**/api/admin/cloudshell/accounts", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      summary: { total_accounts: 10, active_accounts: 2, cooldown_accounts: 1, today_units: 9, queue_depth: 0, updated_at: new Date().toISOString() },
      items: Array.from({ length: 10 }, (_, index) => ({
        account_id: `account${index + 3}`, worker_id: `cloudshell-gmail-account${index + 3}`,
        status: index === 0 ? "active" : "idle", health: "healthy", claimed_units: index,
        claimed_tasks: index, failure_count: 0, soft_quota_units: 0,
        last_seen_at: new Date().toISOString(), last_claimed_at: null,
      })),
    }),
  }));
  await page.goto(`${BASE_ORIGIN}/dashboard`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#dashboard-workspace:not(.hidden)");
  const result = await page.evaluate(() => ({
    title: document.title,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    navVisible: !document.querySelector("#dashboard-nav")?.classList.contains("hidden"),
    credits: document.querySelector("#account-credits")?.textContent,
    trafficLines: document.querySelectorAll("#dashboard-traffic-chart polyline").length,
    reportUsers: document.querySelector("#metric-report-users")?.textContent,
    revenue: document.querySelector("#metric-today-revenue")?.textContent,
    cloudshellCards: document.querySelectorAll(".cloudshell-account-card").length,
    cloudshellTotal: document.querySelector("#cloudshell-total-accounts")?.textContent,
    qualityTotal: document.querySelector("#quality-verification-total")?.textContent,
    qualityDeliverable: document.querySelector("#quality-deliverable-rate")?.textContent,
    qualityUnknown: document.querySelector("#quality-unknown-rate")?.textContent,
    qualityReviewed: document.querySelector("#quality-reviewed-count")?.textContent,
    qualityAttention: [...document.querySelectorAll("#quality-attention-list li")].map((item) => item.textContent),
    providerRows: document.querySelectorAll("#provider-quality-body tr").length,
    providerFirst: document.querySelector("#provider-quality-body tr")?.textContent,
    providerProgressBars: document.querySelectorAll("#provider-quality-body tr:first-child .quality-progress-fill").length,
    providerFirstRate: document.querySelector("#provider-quality-body tr:first-child .quality-value")?.textContent,
  }));
  if (result.title !== "运营监控 | Verigo" || result.overflow || !result.navVisible || result.credits !== "无限额度" || result.trafficLines !== 2 || result.reportUsers !== "17" || result.revenue !== "¥29.90" || result.cloudshellCards !== 10 || result.cloudshellTotal !== "10" || result.qualityTotal !== "1,000" || result.qualityDeliverable !== "85.0%" || result.qualityUnknown !== "7.0%" || result.qualityReviewed !== "50" || result.qualityAttention.join("|") !== "一次性邮箱12|收件箱已满8|角色邮箱25|不应回复6" || result.providerRows !== 4 || !result.providerFirst.includes("Gmail") || result.providerProgressBars !== 3 || result.providerFirstRate !== "86.7%") {
    throw new Error(`dashboard: unexpected rendering ${JSON.stringify(result)}`);
  }
  await page.close();
  return { dashboard: true };
}

async function checkAdminCredits(browser) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.route("**/api/auth/me", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      id: "admin", email: "admin@example.com", email_verified: true,
      credits: 0, paid_credits: 0, trial_credits: 0, trial_credit_expires_at: null,
      needs_email_binding: false, is_admin: true,
    }),
  }));
  await page.route("**/api/admin/credits/grant", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      email: "customer@example.com", delta: 25, credits: 25,
      paid_credits: 25, reference: "admin_grant:smoke", created_at: new Date().toISOString(),
    }),
  }));
  await page.goto(`${BASE_ORIGIN}/admin/credits`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#admin-credits-workspace:not(.hidden)");
  await page.fill("#admin-credit-email", "customer@example.com");
  await page.fill("#admin-credit-amount", "25");
  await page.click("#admin-credit-submit");
  await page.waitForFunction(() => document.querySelector("#admin-credit-result")?.textContent.includes("25"));
  const result = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    navVisible: !document.querySelector("#admin-credits-nav")?.classList.contains("hidden"),
    success: document.querySelector("#admin-credit-result")?.classList.contains("success"),
  }));
  if (result.overflow || !result.navVisible || !result.success) {
    throw new Error(`admin credits: unexpected rendering ${JSON.stringify(result)}`);
  }
  await page.close();
  return { adminCredits: true };
}

async function checkNotificationCenter(browser) {
  const viewport = process.env.VERIGO_NOTIFICATION_VIEWPORT === "desktop" ? { width: 1440, height: 900 } : { width: 390, height: 844 };
  const page = await browser.newPage({ viewport });
  const now = new Date().toISOString();
  const notifications = [
    { id: "review-1", kind: "verification_review", title: "邮箱复核结果已更新", body: "person@example.com 的复核结果已更新", created_at: now, read_at: null, target_job_id: "notify-job", target_email: "person@example.com", target_result_index: 0 },
    { id: "credit-1", kind: "credit_grant", title: "额度已到账", body: "管理员已向你的账户增加 100 额度。", created_at: now, read_at: null, target_job_id: null, target_email: null, target_result_index: null },
    { id: "payment-1", kind: "payment", title: "充值到账", body: "已到账 100 次验证额度。", created_at: now, read_at: now, target_job_id: null, target_email: null, target_result_index: null },
  ];
  for (let index = 0; index < 32; index += 1) notifications.push({
    id: `history-${index}`, kind: "info", title: `历史通知 ${index + 1}`, body: "这是一条已读历史通知。",
    created_at: new Date(Date.now() - ((index + 1) * 60000)).toISOString(), read_at: now,
    target_job_id: null, target_email: null, target_result_index: null,
  });
  await page.route("**/api/auth/me", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ id: "notify-user", email: "notify@example.com", email_verified: true, credits: 100, paid_credits: 100, trial_credits: 0, trial_credit_expires_at: null, needs_email_binding: false, is_admin: false }) }));
  await page.route("**/api/jobs?offset=0&limit=8", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ total: 0, offset: 0, limit: 8, items: [] }) }));
  await page.route(/\/api\/notifications(?:\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get("offset") || 0); const limit = Number(url.searchParams.get("limit") || 30);
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: notifications.slice(offset, offset + limit), unread_count: notifications.filter((item) => !item.read_at).length, total: notifications.length, offset, limit }) });
  });
  await page.route("**/api/notifications/read", async (route) => {
    notifications.forEach((item) => { item.read_at = item.read_at || now; });
    await route.fulfill({ status: 204, body: "" });
  });
  await page.route("**/api/notifications/*/read", async (route) => {
    const id = route.request().url().split("/").at(-2); const item = notifications.find((entry) => entry.id === id); if (item) item.read_at = now;
    await route.fulfill({ status: 204, body: "" });
  });
  await page.route("**/api/jobs/notify-job", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ id: "notify-job", status: "completed", worker_count: 1, completed: 1, total: 1, progress: 100, summary: { total: 1, deliverable: 1, undeliverable: 0, unknown: 0 }, created_at: now, started_at: now, finished_at: now, download_url: null, error: null, queue_position: null, qq_slow: false }) }));
  await page.route("**/api/jobs/notify-job/results?**", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ total: 1, available: 1, offset: 0, limit: 50, items: [{ email: "person@example.com", deliverable: true, valid: true, progress_state: "completed", original_index: 0, checks: { format: true, domain: true, mx: true, smtp: true }, verification_method: "standard", smtp_result: "250 OK", message: "250 OK", domain_type: "normal" }] }) }));
  await page.route("**/api/jobs/notify-job/results/0/reviewed", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/wallet", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ available_verifications: 100, paid_verifications: 100, paid_verifications_used: 0, cumulative_recharge_fen: 50, remaining_paid_value_yuan: 0.5, paid_used_value_yuan: 0, price_fen_per_100: 50, trial_verifications: 0, usage_daily: [], transactions: [] }) }));
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.click("#notification-button");
  await page.waitForFunction(() => document.querySelectorAll(".notification-item").length === 30);
  if (await page.locator(".notification-date-group").count() < 1) throw new Error("notifications: date grouping is missing");
  await page.click("#notification-settings");
  if (!(await page.locator("#notification-preferences").isVisible())) throw new Error("notifications: preferences panel did not open");
  await page.check("#notification-compact");
  if (!(await page.locator("#notification-menu").evaluate((node) => node.classList.contains("is-compact")))) throw new Error("notifications: compact mode was not applied");
  await page.uncheck("#notification-auto-refresh");
  const savedPreferences = await page.evaluate(() => ({ compact: localStorage.getItem("verigo_notification_compact"), autoRefresh: localStorage.getItem("verigo_notification_auto_refresh") }));
  if (savedPreferences.compact !== "1" || savedPreferences.autoRefresh !== "0") throw new Error("notifications: preferences were not persisted");
  if (process.env.VERIGO_NOTIFICATION_SCREENSHOT) await page.screenshot({ path: process.env.VERIGO_NOTIFICATION_SCREENSHOT });
  await page.click("#notification-reset-preferences");
  if (await page.isChecked("#notification-compact") || !(await page.isChecked("#notification-auto-refresh"))) throw new Error("notifications: preference reset failed");
  await page.click("#notification-settings");
  if ((await page.textContent("#notification-summary")) !== "2 条未读" || await page.locator(".notification-item.is-unread").count() !== 2) throw new Error("notifications: unread state is incorrect");
  if ((await page.textContent("#notification-loaded-summary")) !== "已加载 30 / 35") throw new Error("notifications: pagination summary is incorrect");
  await page.click('[data-notification-filter="unread"]');
  if ((await page.evaluate(() => localStorage.getItem("verigo_notification_filter"))) !== "unread") throw new Error("notifications: selected filter was not persisted");
  if (await page.locator(".notification-item").count() !== 2) throw new Error("notifications: unread filter is incorrect");
  await page.click("#notification-mark-all");
  await page.waitForFunction(() => document.querySelectorAll(".notification-item.is-unread").length === 0);
  if (!(await page.locator("#notification-count").evaluate((node) => node.classList.contains("hidden")))) throw new Error("notifications: badge should clear after mark all");
  if (!(await page.locator(".notification-empty").isVisible())) throw new Error("notifications: filtered empty state is missing");
  await page.click('[data-notification-filter="all"]');
  await page.locator("#notification-list").evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await page.waitForFunction(() => document.querySelectorAll(".notification-item").length === 35);
  await page.locator(".notification-review").click();
  await page.waitForFunction(() => document.querySelector("#result-detail-drawer")?.classList.contains("open"));
  if (!(await page.locator("#notification-menu").evaluate((node) => node.classList.contains("hidden")))) throw new Error("notifications: result navigation must close the panel");
  await page.click("#close-result-detail");
  await page.click("#notification-button");
  await page.click('[data-notification-filter="account"]');
  if (await page.locator(".notification-item").count() !== 2) throw new Error("notifications: account filter is incorrect");
  await page.locator(".notification-credit").click();
  await page.waitForSelector("#wallet-workspace:not(.hidden)");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  if (overflow) throw new Error("notifications: mobile panel causes horizontal overflow");
  await page.close();
  return { notificationCenter: true };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    if (process.env.VERIGO_UI_ONLY_RESULT_DETAILS === "1") {
      console.log(JSON.stringify([await checkResultDetails(browser)]));
      return;
    }
    if (process.env.VERIGO_UI_ONLY_RISK === "1") {
      console.log(JSON.stringify([await checkRiskPresentation(browser)]));
      return;
    }
    if (process.env.VERIGO_UI_ONLY_NOTIFICATION === "1") {
      console.log(JSON.stringify([await checkNotificationCenter(browser)]));
      return;
    }
    if (process.env.VERIGO_UI_ONLY_DASHBOARD === "1") {
      console.log(JSON.stringify([await checkDashboard(browser)]));
      return;
    }
    const desktop = await checkViewport(browser, "desktop", 1440, 900);
    const mobile = await checkViewport(browser, "mobile", 390, 844);
    const riskPresentation = await checkRiskPresentation(browser);
    const interaction = process.env.VERIGO_UI_SKIP_ACCOUNT === "1"
      ? { accountAndImport: "skipped: production bot protection enabled" }
      : await checkAccountAndImport(browser);
    const mobileTrialAction = process.env.VERIGO_UI_SKIP_ACCOUNT === "1"
      ? { mobileTrialAction: "skipped: production bot protection enabled" }
      : await checkMobileTrialAction(browser);
    const englishLocale = await checkEnglishLocale(browser);
    const englishDiscoveryAndDocs = await checkEnglishDiscoveryAndDocs(browser);
    const englishDesktopHeadingAndApiKeys = await checkEnglishDesktopHeadingAndApiKeys(browser);
    const dashboard = await checkDashboard(browser);
    const adminCredits = await checkAdminCredits(browser);
    const notificationCenter = await checkNotificationCenter(browser);
    console.log(JSON.stringify([desktop, mobile, riskPresentation, interaction, mobileTrialAction, englishLocale, englishDiscoveryAndDocs, englishDesktopHeadingAndApiKeys, dashboard, adminCredits, notificationCenter]));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
