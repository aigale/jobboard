import {
  BASE_URL,
  DEMO,
  attachScreenshot,
  createPendingEmployer,
  expect,
  goto,
  guard,
  loginUI,
  test,
  waitForToast,
} from "./helpers";

test.describe("Admin journeys", () => {
  test("admin dashboard, crawler, analytics, billing, and import tabs load", async ({ page }) => {
    await loginUI(page, DEMO.admin);
    await expect(page).toHaveURL(/\/admin\.html$/);
    await expect(page.locator("#tiles > div")).toHaveCount(6);
    await expect(page.locator("#pList")).toBeVisible();
    await expect(page.locator("#list")).not.toHaveText("");

    await page.locator("#tCrawl").click();
    await expect(page.locator("#pCrawl")).toBeVisible();
    await expect(page.locator("#crawlNow")).toBeVisible();
    await expect(page.locator("#crawl")).toContainText("Recent crawls");

    await page.locator("#tStats").click();
    await expect(page.locator("#pStats")).toBeVisible();
    await expect(page.locator("#stTiles .stat")).toHaveCount(6);
    await expect(page.locator("#chVisits svg")).toBeVisible();

    await page.locator("#tBill").click();
    await expect(page.locator("#pBill")).toBeVisible();
    await expect(page.locator("#billBody")).toContainText("Prices");
    await expect(page.locator("#billNotice")).toContainText("test environment");

    await page.locator("#tImport").click();
    await expect(page.locator("#pImport")).toBeVisible();
    await expect(page.locator("#feedForm")).toBeVisible();
    await expect(page.locator("#impForm")).toBeVisible();
    await expect(page.locator("#feeds")).not.toHaveText("");
    await attachScreenshot(page, "admin-import", false);
  });

  test("admin approves a new employer, then that employer can sign in", async ({
    page,
    request,
    browser,
  }) => {
    const pending = await createPendingEmployer(request);

    await loginUI(page, DEMO.admin);
    await page.locator("#tPending").click();
    await expect(page.locator("#pList")).toBeVisible();
    const row = page.locator("#list .rowcard", { hasText: pending.companyName }).first();
    await expect(row).toBeVisible();
    await expect(row).toContainText("Pending");
    await attachScreenshot(page, "admin-pending-company", false);

    await row.locator("button[data-approve]").click();
    await waitForToast(page, "Approved. The employer was notified.");
    await expect(
      page.locator("#list .rowcard", { hasText: pending.companyName }),
    ).toHaveCount(0);

    await page.locator("#tApproved").click();
    const approved = page.locator("#list .rowcard", { hasText: pending.companyName }).first();
    await expect(approved).toBeVisible();
    await expect(approved).toContainText("Live");

    const employerContext = await browser.newContext({ baseURL: BASE_URL });
    const employerPage = await employerContext.newPage();
    const employerErrors = await guard(employerPage);
    await loginUI(employerPage, pending.email, pending.password);
    await expect(employerPage).toHaveURL(/\/employer\.html$/);
    await expect(employerPage.locator("#coName")).toContainText(pending.companyName);
    await expect(employerPage.locator("#banner")).not.toContainText("We're checking");
    await expect(employerPage.locator("#vbanner")).toContainText("Confirm your email.");
    expect(employerErrors).toEqual([]);
    await attachScreenshot(employerPage, "new-employer-approved", false);
    await employerContext.close();
  });
});
