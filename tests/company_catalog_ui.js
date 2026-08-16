const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const baseUrl = process.env.VERIGO_COMPANY_UI_BASE || "http://127.0.0.1:8765/static/index.html";
const screenshotDir = process.env.VERIGO_COMPANY_UI_SCREENSHOT_DIR;

const company = {
  id: "company-1",
  name: "! boost-your-sales !",
  name_display: "Boost-Your-Sales",
  website: "boost-your-sales.eu",
  website_url: "https://boost-your-sales.eu",
  website_domain: "boost-your-sales.eu",
  linkedin_url: "https://www.linkedin.com/company/boost-your-sales",
  logo_url: "",
  country: "germany",
  country_label: "德国",
  region: "north rhine-westphalia",
  region_label: "北莱茵-威斯特法伦州",
  locality: "oberhausen",
  locality_label: "Oberhausen",
  location_label: "德国 · 北莱茵-威斯特法伦州 · Oberhausen",
  industry: "management consulting",
  industry_label: "管理咨询",
  size: "51-200",
  size_label: "51–200 人",
  vitality_state: "active_verified",
  vitality_queue_state: "",
  vitality_confidence: 0.91,
  vitality_checked_at: "2026-08-16T08:00:00+00:00",
  vitality_reason: "website_identity_match",
};

async function mockApi(page) {
  const counters = { search: 0, publicSearch: 0 };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let payload = {};
    if (url.pathname === "/api/auth/me") {
      payload = { id: "admin", email: "admin@example.com", is_admin: true, email_verified: true };
    } else if (url.pathname === "/api/admin/accounts/list") {
      payload = { total: 0, offset: 0, limit: 50, items: [], summary: {} };
    } else if (url.pathname === "/api/admin/feature-usage") {
      payload = { daily: [], totals: { single: 0, batch: 0, discovery: 0 } };
    } else if (url.pathname.endsWith("/facets/country")) {
      payload = { items: [{ value: "germany", label: "德国", count: 1168694 }] };
    } else if (url.pathname.endsWith("/facets/industry")) {
      payload = { items: [{ value: "management consulting", label: "管理咨询", count: 1196975 }] };
    } else if (url.pathname.endsWith("/company-catalog/search")) {
      counters.search += 1;
      payload = { total: 1, offset: 0, limit: 25, has_more: false, items: [company] };
    } else if (url.pathname === "/api/company-finder/search") {
      counters.publicSearch += 1;
      payload = { total: 1, offset: 0, limit: 25, has_more: false, pending_count: 2, refresh_after_seconds: 4, items: [company] };
    } else if (url.pathname === "/api/company-finder/companies/company-1") {
      payload = { ...company, vitality_evidence: { type: "官网公开证据", page_title: "Boost Your Sales", reason: "website_identity_match" } };
    } else if (url.pathname === "/api/notifications") {
      payload = { items: [], unread: 0 };
    } else if (url.pathname === "/api/public/config") {
      payload = {};
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
  });
  return counters;
}

async function checkPublicFinderViewport(browser, name, viewport) {
  const page = await browser.newPage({ viewport });
  await page.addInitScript(() => history.replaceState({}, "", "/app/company-finder"));
  const counters = await mockApi(page);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelectorAll("#company-finder-country option").length > 1);
  await page.click("#company-finder-form button[type=submit]");
  await page.waitForFunction(() => document.querySelector("#company-finder-status")?.textContent.includes("至少一个筛选条件"));
  if (counters.publicSearch !== 0) throw new Error(`${name}: an empty public search reached the API`);
  await page.selectOption("#company-finder-country", "germany");
  await page.click("#company-finder-form button[type=submit]");
  await page.waitForFunction(() => document.querySelector("#company-finder-results")?.textContent.includes("Boost-Your-Sales"));
  const result = await page.evaluate(() => ({
    text: document.querySelector("#company-finder-results")?.textContent || "",
    pending: document.querySelector("#company-finder-pending-copy")?.textContent || "",
    websiteHref: document.querySelector(".company-finder-website")?.href,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  }));
  if (!result.text.includes("Boost-Your-Sales") || !result.text.includes("德国 · 北莱茵-威斯特法伦州 · Oberhausen")) {
    throw new Error(`${name}: public result is incomplete: ${result.text}`);
  }
  if (result.text.includes("active_verified") || result.text.includes("management consulting")) {
    throw new Error(`${name}: raw internal catalogue data leaked: ${result.text}`);
  }
  if (!result.pending.includes("正在完成公开网站检查")) {
    throw new Error(`${name}: vitality checking state is not explained`);
  }
  if (result.websiteHref !== "https://boost-your-sales.eu/") throw new Error(`${name}: public website link is incorrect`);
  if (result.overflow) throw new Error(`${name}: public finder has horizontal overflow`);
  await page.click(".company-finder-details");
  await page.waitForFunction(() => document.querySelector("#company-finder-detail-title")?.textContent.includes("Boost-Your-Sales"));
  const detail = await page.evaluate(() => ({
    text: document.querySelector("#company-finder-detail-content")?.textContent || "",
    status: document.querySelector("#company-finder-detail-status")?.textContent || "",
    website: document.querySelector("#company-finder-detail-actions a")?.href,
  }));
  if (!detail.text.includes("管理咨询") || !detail.text.includes("官网公开证据") || detail.status !== "近期可确认") {
    throw new Error(`${name}: company detail drawer is incomplete: ${JSON.stringify(detail)}`);
  }
  if (detail.website !== "https://boost-your-sales.eu/") throw new Error(`${name}: company detail website is incorrect`);
  await page.click("#close-company-finder-detail");
  if (screenshotDir) {
    fs.mkdirSync(screenshotDir, { recursive: true });
    await page.locator("#company-finder-workspace").screenshot({ path: path.join(screenshotDir, `${name}.png`) });
  }
  await page.click(".company-finder-handoff");
  await page.waitForFunction(() => window.location.pathname === "/app/finder");
  if (await page.inputValue("#discovery-domain") !== "boost-your-sales.eu") {
    throw new Error(`${name}: company-to-domain handoff failed`);
  }
  await page.close();
  return { name, ...result };
}

async function checkViewport(browser, name, viewport) {
  const page = await browser.newPage({ viewport });
  await page.addInitScript(() => history.replaceState({}, "", "/admin/credits"));
  const counters = await mockApi(page);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelectorAll("#company-catalog-country option").length > 1);
  if (await page.inputValue("#company-catalog-website") !== "true") {
    throw new Error(`${name}: website-only filter is not selected by default`);
  }
  await page.click("#company-catalog-form button[type=submit]");
  await page.waitForFunction(() => document.querySelector("#company-catalog-status")?.textContent.includes("至少一个筛选条件"));
  if (counters.search !== 0) throw new Error(`${name}: an empty search reached the API`);
  await page.selectOption("#company-catalog-country", "germany");
  await page.selectOption("#company-catalog-industry", "management consulting");
  await page.click("#company-catalog-form button[type=submit]");
  await page.waitForFunction(() => document.querySelector("#company-catalog-status")?.textContent === "查询完成");

  const result = await page.evaluate(() => {
    const table = document.querySelector("#company-catalog-results");
    const linkedin = table.querySelector(".company-catalog-linkedin");
    const website = table.querySelector(".company-catalog-website");
    return {
      text: table.textContent,
      linkedinLabel: linkedin?.getAttribute("aria-label"),
      linkedinRel: linkedin?.getAttribute("rel"),
      linkedinIcon: linkedin?.querySelector("i")?.className,
      websiteHref: website?.href,
      documentOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      formColumns: getComputedStyle(document.querySelector("#company-catalog-form")).gridTemplateColumns.split(" ").length,
    };
  });
  if (!result.text.includes("Boost-Your-Sales") || !result.text.includes("德国 · 北莱茵-威斯特法伦州 · Oberhausen")) {
    throw new Error(`${name}: normalized company or location is missing: ${result.text}`);
  }
  if (!result.text.includes("管理咨询") || !result.text.includes("51–200 人")) {
    throw new Error(`${name}: localized industry or size is missing: ${result.text}`);
  }
  if (!result.text.includes("近期可确认")) {
    throw new Error(`${name}: company vitality status is missing: ${result.text}`);
  }
  if (result.text.includes("management consulting") || result.text.includes("Open")) {
    throw new Error(`${name}: raw catalogue text leaked into the table: ${result.text}`);
  }
  if (result.linkedinLabel !== "打开 LinkedIn 公司主页" || !result.linkedinRel.includes("noopener")
    || !result.linkedinIcon.includes("fa-linkedin-in")) {
    throw new Error(`${name}: LinkedIn icon link is incomplete`);
  }
  if (result.websiteHref !== "https://boost-your-sales.eu/") {
    throw new Error(`${name}: normalized website link is incorrect: ${result.websiteHref}`);
  }
  if (result.documentOverflow) throw new Error(`${name}: page has horizontal overflow`);
  if (viewport.width <= 560 && result.formColumns !== 1) {
    throw new Error(`${name}: mobile filter form is not single-column`);
  }
  if (screenshotDir) {
    fs.mkdirSync(screenshotDir, { recursive: true });
    await page.locator(".company-catalog-card").screenshot({ path: path.join(screenshotDir, `${name}.png`) });
  }
  await page.close();
  return { name, ...result };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const results = [];
    results.push(await checkViewport(browser, "company-catalog-desktop", { width: 1440, height: 1000 }));
    results.push(await checkViewport(browser, "company-catalog-mobile", { width: 390, height: 844 }));
    results.push(await checkPublicFinderViewport(browser, "company-finder-desktop", { width: 1440, height: 1000 }));
    results.push(await checkPublicFinderViewport(browser, "company-finder-mobile", { width: 390, height: 844 }));
    console.log(JSON.stringify(results, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
