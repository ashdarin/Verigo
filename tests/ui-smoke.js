const { chromium } = require("playwright");

async function checkViewport(browser, name, width, height) {
  const page = await browser.newPage({ viewport: { width, height } });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto("http://127.0.0.1:8000", { waitUntil: "networkidle" });

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
    };
  });

  if (!result.title.includes("Verigo")) throw new Error(`${name}: unexpected title`);
  if (result.overflow) throw new Error(`${name}: page has horizontal overflow`);
  if (!result.startVisible || !result.progressVisible || !result.tableVisible || result.apiDocsHref !== "/api-docs") {
    throw new Error(`${name}: a primary UI region is hidden`);
  }
  if (errors.length) throw new Error(`${name}: console errors: ${errors.join(" | ")}`);
  await page.close();
  return { name, ...result };
}

async function checkAccountAndImport(browser) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.goto("http://127.0.0.1:8000", { waitUntil: "networkidle" });
  await page.click("#account-button");
  await page.click('[data-auth-mode="register"]');
  const email = `ui_${Date.now()}@example.com`;
  await page.fill("#auth-email", email);
  await page.fill("#auth-password", "browser-smoke-2026");
  await page.click("#auth-submit");
  await page.waitForFunction((value) => document.querySelector("#account-button")?.textContent === value, email);
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
  await page.click("#account-button");
  await page.click("#api-keys-button");
  if (!(await page.locator("#api-keys-dialog").evaluate((node) => node.open))) {
    throw new Error("api keys: management must be available from the signed-in account menu");
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
  await page.goto("http://127.0.0.1:8000", { waitUntil: "networkidle" });
  await page.click("#account-button");
  await page.click('[data-auth-mode="register"]');
  await page.fill("#auth-email", `mobile_${Date.now()}@example.com`);
  await page.fill("#auth-password", "browser-smoke-2026");
  await page.click("#auth-submit");
  await page.waitForFunction(() => !document.querySelector("#claim-trial-button")?.classList.contains("hidden"));
  await page.click("#account-button");
  await page.click("#api-keys-button");
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
  await page.goto("http://127.0.0.1:8000", { waitUntil: "networkidle" });
  await page.click("#locale-toggle");
  await page.fill("#single-email-input", "locale-check@yahoo.com");
  await page.click("#start-button");
  await page.waitForFunction(() => document.querySelectorAll("#results-body td").length >= 5);
  const result = await page.evaluate(() => ({
    lang: document.documentElement.lang,
    code: document.querySelector("#locale-code")?.textContent,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    chinese: [...document.querySelectorAll("body *")]
      .filter((node) => node.children.length === 0 && /[\u4e00-\u9fff]/.test(node.textContent || ""))
      .filter((node) => getComputedStyle(node).display !== "none")
      .map((node) => node.textContent.trim())
      .filter(Boolean),
    values: [...document.querySelectorAll("#results-body td")].map((node) => node.textContent.trim()),
  }));
  if (result.lang !== "en" || result.code !== "EN" || result.overflow || result.chinese.length) {
    throw new Error(`english locale: unexpected rendering ${JSON.stringify(result)}`);
  }
  if (!result.values.includes("Unsupported validation")) {
    throw new Error(`english locale: result detail was not localized ${JSON.stringify(result.values)}`);
  }
  await page.close();
  return { englishLocale: true };
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
      totals: { page_views: 200, unique_visitors: 80, users: 31, verified_users: 20, jobs: 50, revenue_fen: 5990, paid_orders: 4 },
      jobs: { queued: 1, running: 2, completed: 45, failed: 2 },
      daily: Array.from({ length: 14 }, (_, index) => ({
        day: `2026-07-${String(index + 1).padStart(2, "0")}`, page_views: index + 1,
        unique_visitors: index + 1, engaged_sessions: Math.max(0, index - 1),
      })),
    }),
  }));
  await page.goto("http://127.0.0.1:8000/dashboard", { waitUntil: "networkidle" });
  await page.waitForSelector("#dashboard-workspace:not(.hidden)");
  const result = await page.evaluate(() => ({
    title: document.title,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    navVisible: !document.querySelector("#dashboard-nav")?.classList.contains("hidden"),
    credits: document.querySelector("#account-credits")?.textContent,
    trafficLines: document.querySelectorAll("#dashboard-traffic-chart polyline").length,
    reportUsers: document.querySelector("#metric-report-users")?.textContent,
    revenue: document.querySelector("#metric-today-revenue")?.textContent,
  }));
  if (result.title !== "运营监控 | Verigo" || result.overflow || !result.navVisible || result.credits !== "无限额度" || result.trafficLines !== 2 || result.reportUsers !== "17" || result.revenue !== "¥29.90") {
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
  await page.goto("http://127.0.0.1:8000/admin/credits", { waitUntil: "networkidle" });
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

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const desktop = await checkViewport(browser, "desktop", 1440, 900);
    const mobile = await checkViewport(browser, "mobile", 390, 844);
    const interaction = await checkAccountAndImport(browser);
    const mobileTrialAction = await checkMobileTrialAction(browser);
    const englishLocale = await checkEnglishLocale(browser);
    const dashboard = await checkDashboard(browser);
    const adminCredits = await checkAdminCredits(browser);
    console.log(JSON.stringify([desktop, mobile, interaction, mobileTrialAction, englishLocale, dashboard, adminCredits]));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
