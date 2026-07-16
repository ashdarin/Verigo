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
    };
  });

  if (result.title !== "Verigo") throw new Error(`${name}: unexpected title`);
  if (result.overflow) throw new Error(`${name}: page has horizontal overflow`);
  if (!result.startVisible || !result.progressVisible || !result.tableVisible) {
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
  if (!(await page.textContent("#discovery-verify")).includes("额度")) {
    throw new Error("discovery: paid candidate verification must show its credit cost");
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
  if (await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)) {
    throw new Error("mobile trial action: page has horizontal overflow after login");
  }
  await page.close();
  return { mobileTrialAction: true };
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
      today: { page_views: 42, unique_visitors: 17, new_users: 3, new_jobs: 5, credits_consumed: 12, revenue_fen: 2990, paid_orders: 2 },
      totals: { page_views: 200, unique_visitors: 80, users: 31, verified_users: 20, jobs: 50, revenue_fen: 5990, paid_orders: 4 },
      jobs: { queued: 1, running: 2, completed: 45, failed: 2 },
      daily: Array.from({ length: 7 }, (_, index) => ({ day: `2026-07-${String(index + 10).padStart(2, "0")}`, page_views: index + 1, unique_visitors: index + 1 })),
    }),
  }));
  await page.goto("http://127.0.0.1:8000/dashboard", { waitUntil: "networkidle" });
  await page.waitForSelector("#dashboard-workspace:not(.hidden)");
  const result = await page.evaluate(() => ({
    title: document.title,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    navVisible: !document.querySelector("#dashboard-nav")?.classList.contains("hidden"),
    trafficRows: document.querySelectorAll("#dashboard-traffic-body tr").length,
    revenue: document.querySelector("#metric-today-revenue")?.textContent,
  }));
  if (result.title !== "运营监控 | Verigo" || result.overflow || !result.navVisible || result.trafficRows !== 7 || result.revenue !== "¥29.90") {
    throw new Error(`dashboard: unexpected rendering ${JSON.stringify(result)}`);
  }
  await page.close();
  return { dashboard: true };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const desktop = await checkViewport(browser, "desktop", 1440, 900);
    const mobile = await checkViewport(browser, "mobile", 390, 844);
    const interaction = await checkAccountAndImport(browser);
    const mobileTrialAction = await checkMobileTrialAction(browser);
    const dashboard = await checkDashboard(browser);
    console.log(JSON.stringify([desktop, mobile, interaction, mobileTrialAction, dashboard]));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
