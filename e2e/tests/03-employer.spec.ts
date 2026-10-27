import {
  BASE_URL,
  DEMO,
  attachScreenshot,
  ensureSeekerApplication,
  expect,
  goto,
  guard,
  loginUI,
  test,
  waitForToast,
} from "./helpers";

test.describe("Employer journeys", () => {
  test("employer sees an Easy Apply applicant and moves the stage", async ({
    page,
    request,
    browser,
  }) => {
    await ensureSeekerApplication(request);
    await loginUI(page, DEMO.employer);
    await expect(page).toHaveURL(/\/employer\.html$/);
    await expect(page.locator("#coName")).toHaveText("Sinag Demo Corp");
    await expect(page.locator("#pJobs")).toBeVisible();
    await expect(page.locator("#jobs .rowcard")).toHaveCount(3);
    await expect(page.locator("#jobs")).toContainText("Bookkeeper (Part-time)");

    await page.locator("#tPipe").click();
    await expect(page.locator("#pPipe")).toBeVisible();
    const bookkeeper = page.locator("#pipeJob option", { hasText: "Bookkeeper" }).first();
    const bookkeeperId = await bookkeeper.getAttribute("value");
    expect(bookkeeperId).toBeTruthy();
    await page.locator("#pipeJob").selectOption(bookkeeperId!);
    await expect(page.locator("#kanban .kcard", { hasText: "Juan Dela Cruz" })).toBeVisible();
    await page.locator("#kanban .kcard", { hasText: "Juan Dela Cruz" }).first().click();

    const drawer = page.locator("#drawerRoot aside.drawer");
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("seeker@demo.sinag");
    await expect(page.locator("#dStage")).toBeVisible();
    await page.locator("#dStage").selectOption("shortlisted");
    await page.locator("#dMove").click();
    await waitForToast(page, /Moved to Shortlisted/);
    await expect(page.locator("#dStage")).toHaveValue("shortlisted");
    await page.locator("#dClose").click();
    await expect(page.locator("#drawerRoot aside.drawer")).toHaveCount(0);

    const seekerContext = await browser.newContext({ baseURL: BASE_URL });
    const seekerPage = await seekerContext.newPage();
    const seekerErrors = await guard(seekerPage);
    await loginUI(seekerPage, DEMO.seeker);
    await goto(seekerPage, "/seeker.html#applications");
    const application = seekerPage
      .locator("#apps .rowcard", { hasText: "Bookkeeper (Part-time)" })
      .first();
    await expect(application).toBeVisible();
    await expect(application.locator(".status")).toHaveText("Shortlisted");
    expect(seekerErrors).toEqual([]);
    await seekerContext.close();
  });

  test("employer can save a draft job and update the public company page", async ({
    page,
  }) => {
    await loginUI(page, DEMO.employer);
    await goto(page, "/employer.html#jobs");
    await expect(page.locator("#pJobs")).toBeVisible();
    await expect(page.locator("#jobs .rowcard")).toHaveCount(3);

    await page.locator("#tEdit").click();
    await expect(page.locator("#pEdit")).toBeVisible();
    const stamp = Date.now().toString(36);
    const title = `E2E Draft Role ${stamp}`;
    await page.locator("#jTitle").fill(title);
    await page.locator("#jCity").fill("Cebu City");
    await page.locator("#jDesc").fill(
      "This is a temporary job created by the end-to-end test suite. " +
        "It checks that an employer can save a draft without publishing.",
    );
    await page.locator('input[name="when"][value="draft"]').check();
    await expect(page.locator("#jSave")).toHaveText("Save draft");
    await page.locator("#jSave").click();
    await waitForToast(page, "Draft saved");

    await expect(page.locator("#pJobs")).toBeVisible();
    const draft = page.locator("#jobs .rowcard", { hasText: title }).first();
    await expect(draft).toBeVisible();
    await expect(draft.locator(".status")).toHaveText("Draft");

    await page.locator("#tCompany").click();
    await expect(page.locator("#pCompany")).toBeVisible();
    await expect(page.locator("#brandForm")).toBeVisible();
    const about = `E2E company description ${stamp}`;
    await page.locator("#cAbout").fill(about);
    await page.locator("#cSave").click();
    await waitForToast(page, "Company saved");

    await page.locator("#tCompany").click();
    await expect(page.locator("#brandForm")).toBeVisible();
    const tagline = `E2E tagline ${stamp}`;
    await page.locator("#bTag").fill(tagline);
    await page.locator("#brandForm button[type=submit]").click();
    await waitForToast(page, "Company page saved");

    await goto(page, "/company.html?c=sinag-demo-corp");
    await expect(page.locator("main h1")).toContainText("Sinag Demo Corp");
    await expect(page.locator("main")).toContainText(about);
    await expect(page.locator("main")).toContainText(tagline);
    await expect(page.locator("main")).toContainText("Verified employer");
    await attachScreenshot(page, "public-company-page");
  });

  test("analytics, team, and skills-test tabs load with data", async ({ page }) => {
    await loginUI(page, DEMO.employer);

    await page.locator("#tStats").click();
    await expect(page.locator("#pStats")).toBeVisible();
    await expect(page.locator("#stTiles .stat")).toHaveCount(4);
    await expect(page.locator("#chViews svg")).toBeVisible();

    await page.locator("#tTeam").click();
    await expect(page.locator("#pTeam")).toBeVisible();
    await expect(page.locator("#members")).toContainText("Maria Santos");
    await expect(page.locator("#members")).toContainText("employer@demo.sinag");

    await page.locator("#tTests").click();
    await expect(page.locator("#pTests")).toBeVisible();
    await expect(page.locator("#testList")).toContainText("Customer support basics");
  });
});
