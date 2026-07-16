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
  const username = `ui_${Date.now()}`;
  await page.fill("#auth-username", username);
  await page.fill("#auth-password", "browser-smoke-2026");
  await page.click("#auth-submit");
  await page.waitForFunction((value) => document.querySelector("#account-button")?.textContent === value, username);
  if (await page.locator("#recent-block").evaluate((node) => node.classList.contains("hidden"))) {
    throw new Error("account: recent jobs should be visible after login");
  }

  await page.click('[data-mode="file"]');
  await page.setInputFiles("#file-input", {
    name: "contacts.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("name,email\nA,first@example.com\nB,second@example.cn"),
  });
  await page.waitForFunction(() => document.querySelector("#email-count")?.textContent === "2");
  await page.close();
  return { account: true, importCount: 2 };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const desktop = await checkViewport(browser, "desktop", 1440, 900);
    const mobile = await checkViewport(browser, "mobile", 390, 844);
    const interaction = await checkAccountAndImport(browser);
    console.log(JSON.stringify([desktop, mobile, interaction]));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
