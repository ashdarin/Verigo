const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const baseUrl = process.env.VERIGO_LIVE_BASE || "https://verigo.site";
const outputDir = path.resolve(process.env.VERIGO_SCREENSHOT_DIR || "snapshots/auth-regression-20260809");
const stamp = Date.now();
const email = process.env.VERIGO_LIVE_EMAIL || `live-regression-${stamp}@verigo.site`;
const password = process.env.VERIGO_LIVE_PASSWORD || `Verigo-live-${stamp}-A9!`;
const results = [];
let browser;

function record(name, detail = "ok") {
  results.push({ name, detail });
}

async function screenshot(page, name, fullPage = true) {
  await page.screenshot({ path: path.join(outputDir, name), fullPage });
}

async function closeOnboarding(page) {
  const dialog = page.locator("#onboarding-dialog");
  if (await dialog.count() && await dialog.evaluate((node) => node.open)) {
    await page.locator("#onboarding-dialog [data-close-onboarding]").first().click();
  }
}

async function signIn(page) {
  await page.click("#account-button");
  await page.fill("#auth-email", email);
  await page.fill("#auth-password", password);
  await page.click("#auth-submit");
  await page.waitForFunction((value) => document.querySelector("#account-button")?.textContent === value, email);
  await closeOnboarding(page);
}

(async () => {
  fs.mkdirSync(outputDir, { recursive: true });
  fs.rmSync(path.join(outputDir, "regression-error.txt"), { force: true });
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, locale: "zh-CN" });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });

  await page.goto(`${baseUrl}/verify`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#account-button");
  const registrationStatus = await page.evaluate(async ({ email, password }) => {
    const response = await fetch("/api/auth/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: `blocked-${Date.now()}@verigo.site`, password, turnstile_token: "" }),
    });
    return response.status;
  }, { email, password });
  if (registrationStatus !== 403) throw new Error(`Turnstile registration boundary returned ${registrationStatus}`);
  record("registration-turnstile-boundary", "403");
  consoleErrors.length = 0;

  await signIn(page);
  record("login");

  await page.click("#account-button");
  await page.click("#logout-button");
  await page.waitForFunction(() => document.querySelector("#account-button")?.textContent === "登录");
  record("logout");
  await signIn(page);
  record("re-login");

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction((value) => document.querySelector("#account-button")?.textContent === value, email);
  await closeOnboarding(page);
  record("session-persistence");

  await page.click("#account-button");
  await page.click("#workspace-nav");
  await page.waitForSelector("#workspace-home:not(.hidden)");
  await screenshot(page, "01-desktop-account-overview.png");
  record("account-overview");

  await page.click('[data-view="single"]');
  await page.fill("#single-email-input", "support@gmail.com");
  await page.fill("#single-list-name-input", "线上登录态回归");
  await page.click("#start-button");
  await page.waitForFunction(() => document.querySelector("#job-status")?.textContent !== "未开始");
  await screenshot(page, "02-desktop-verification-running.png", false);
  await page.waitForFunction(() => document.querySelector("#job-status")?.textContent === "已完成", null, { timeout: 90000 });
  await page.waitForFunction(() => document.querySelectorAll("#results-body tr:not(.empty-row)").length > 0);
  await page.waitForFunction(() => !["验证中", "等待验证"].includes(document.querySelector("#results-body tr td:nth-child(2)")?.textContent.trim()));
  await screenshot(page, "03-desktop-verification-result.png", false);
  const resultState = await page.locator("#results-body tr").first().innerText();
  record("single-verification", resultState.replace(/\s+/g, " ").trim());

  await page.click('[data-view="history"]');
  await page.waitForSelector("#history-workspace:not(.hidden)");
  await page.waitForFunction(() => document.querySelectorAll("#history-list .history-item").length > 0);
  await screenshot(page, "04-desktop-history.png");
  record("history");

  await page.click('[data-view="batch"]');
  await page.fill("#list-name-input", "回归测试名单");
  await page.fill("#email-input", "support@gmail.com\nhello@outlook.com\ninvalid@example.invalid");
  await page.waitForFunction(() => document.querySelector("#email-count")?.textContent === "3");
  await screenshot(page, "05-desktop-bulk-ready.png", false);
  record("bulk-ready");

  await page.click('[data-view="discovery"]');
  await page.fill("#discovery-first-name", "Satya");
  await page.fill("#discovery-last-name", "Nadella");
  await page.fill("#discovery-domain", "microsoft.com");
  await page.click("#discovery-start");
  await page.waitForFunction(() => document.querySelectorAll("#discovery-candidates span").length > 0);
  await screenshot(page, "06-desktop-finder-results.png");
  record("finder");

  await page.click("#account-button");
  await page.click("#wallet-nav");
  await page.waitForSelector("#wallet-workspace:not(.hidden)");
  await screenshot(page, "07-desktop-billing.png");
  record("billing");

  await page.click("#api-nav");
  await page.waitForFunction(() => document.querySelector("#api-keys-dialog")?.open);
  await page.fill("#api-key-name", "live regression");
  await page.click("#api-key-create-submit");
  await page.waitForFunction(() => document.querySelector("#api-key-token")?.value.startsWith("vg_live_"));
  await page.click("#close-api-keys");
  await page.click("#api-nav");
  await page.waitForFunction(() => document.querySelectorAll("#api-keys-list .api-key-row").length > 0);
  await screenshot(page, "08-desktop-api-keys.png", false);
  await page.locator("#api-keys-list .api-key-row .account-delete").first().click();
  record("api-key-create-and-revoke");
  await page.click("#close-api-keys");

  const adminStatus = await page.evaluate(async () => (await fetch("/api/admin/metrics")).status);
  if (adminStatus !== 403) throw new Error(`non-admin metrics access returned ${adminStatus}`);
  record("admin-boundary", "403");
  consoleErrors.length = 0;

  const mobile = await context.newPage();
  await mobile.setViewportSize({ width: 390, height: 844 });
  await mobile.goto(`${baseUrl}/verify`, { waitUntil: "domcontentloaded" });
  await mobile.waitForFunction((value) => document.querySelector("#account-button")?.textContent === value, email);
  await closeOnboarding(mobile);
  await mobile.click("#account-button");
  await mobile.click("#workspace-nav");
  await mobile.waitForSelector("#workspace-home:not(.hidden)");
  await screenshot(mobile, "09-mobile-account-overview.png");
  await mobile.click('[data-view="history"]');
  await mobile.waitForSelector("#history-workspace:not(.hidden)");
  await mobile.waitForFunction(() => document.querySelectorAll("#history-list .history-item").length > 0);
  await screenshot(mobile, "10-mobile-history.png");
  const overflow = await mobile.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  if (overflow) throw new Error("mobile page has horizontal overflow");
  record("mobile-responsive");

  if (consoleErrors.length) throw new Error(`console errors: ${consoleErrors.join(" | ")}`);
  fs.writeFileSync(path.join(outputDir, "regression-summary.json"), JSON.stringify({ baseUrl, email, results }, null, 2));
  await browser.close();
  process.stdout.write(JSON.stringify({ outputDir, email, results }, null, 2));
})().catch((error) => {
  fs.mkdirSync(outputDir, { recursive: true });
  fs.writeFileSync(path.join(outputDir, "regression-error.txt"), String(error.stack || error));
  console.error(error);
  if (browser) browser.close().catch(() => {});
  process.exitCode = 1;
});
