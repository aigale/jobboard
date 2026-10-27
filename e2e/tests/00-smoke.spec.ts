import {
  attachScreenshot,
  expect,
  goto,
  test,
  waitForBoard,
} from "./helpers";

test.describe("Cross-browser smoke", () => {
  test("home board loads, searches, and filters", async ({ page }, testInfo) => {
    await goto(page, "/");
    await waitForBoard(page);

    const initialCards = await page.locator("#jobs .job").count();
    expect(initialCards).toBeGreaterThan(0);

    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().includes("/api/jobs?") &&
          new URL(response.url()).searchParams.get("q") === "Python",
      ),
      page.locator("#q").fill("Python"),
    ]);
    await expect(page.locator("#jobs .job").first()).toContainText(/Python/i);

    await Promise.all([
      page.waitForResponse((response) => {
        if (!response.url().includes("/api/jobs?")) return false;
        const params = new URL(response.url()).searchParams;
        return !params.has("q") && !params.has("setup");
      }),
      page.locator("#reset").click(),
    ]);
    await waitForBoard(page);
    await expect(page.locator("#q")).toHaveValue("");

    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().includes("/api/jobs?") &&
          new URL(response.url()).searchParams.get("setup") === "Remote",
      ),
      page.locator('#fSetup input[value="Remote"]').check(),
    ]);
    await expect(page.locator("#jobs .job").first()).toBeVisible();
    await expect
      .poll(async () => {
        const cards = await page.locator("#jobs .job").count();
        const remotes = await page.locator("#jobs .job .meta span.pill.remote").count();
        return cards > 0 && cards === remotes;
      })
      .toBeTruthy();
    const remotePills = await page
      .locator("#jobs .job .meta span.pill.remote")
      .allTextContents();
    expect(remotePills.every((text) => text.trim() === "Remote")).toBeTruthy();

    await attachScreenshot(
      page,
      `smoke-board-${testInfo.project.name}`,
    );
  });

  test("light and dark themes stay distinct and persist", async ({ page }, testInfo) => {
    await goto(page, "/");
    await waitForBoard(page);
    await page.evaluate(() => document.fonts.ready);

    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    const lightBackground = await page.evaluate(
      () => getComputedStyle(document.body).backgroundColor,
    );
    const bodyText = await page.locator("body").innerText();

    await page.locator('[data-theme-set="dark"]').click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const darkBackground = await page.evaluate(
      () => getComputedStyle(document.body).backgroundColor,
    );

    expect(darkBackground).not.toBe(lightBackground);
    expect(await page.locator("body").innerText()).toBe(bodyText);
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem("sinag_theme")))
      .toBe("dark");

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    await page.locator('[data-theme-set="light"]').click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await attachScreenshot(page, `smoke-theme-light-${testInfo.project.name}`);
  });
});
