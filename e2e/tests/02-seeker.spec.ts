import fs from "node:fs";
import path from "node:path";
import {
  DEMO,
  attachScreenshot,
  expect,
  goto,
  loginUI,
  test,
  waitForBoard,
  waitForToast,
} from "./helpers";

const RESUME = path.resolve(__dirname, "..", "..", "static", "icon-192.png");

test.describe("Job seeker journeys", () => {
  test("demo seeker can sign in and sees a working dashboard", async ({ page }) => {
    await loginUI(page, DEMO.seeker);
    await expect(page).toHaveURL(/\/seeker\.html$/);
    await expect(page.locator("#hello")).toHaveText("Hi, Juan");
    await expect(page.locator("#verifyNotice")).toHaveCount(0);
    await expect(page.locator("#pApps")).toBeVisible();
    await expect(page.locator("#apps")).toContainText("No applications yet.");
    await expect(page.locator("#tProfile")).toBeVisible();
    await expect(page.locator("#tFiles")).toBeVisible();
    await expect(page.locator("#tAlerts")).toBeVisible();
  });

  test("seeker can upload a resume, Easy Apply, and track the application", async ({ page }) => {
    await loginUI(page, DEMO.seeker);
    await goto(page, "/seeker.html#files");
    await expect(page.locator("#pFiles")).toBeVisible();
    await page.locator("#upKind").selectOption("resume");
    await page.locator("#upFile").setInputFiles({
      name: "e2e-resume.png",
      mimeType: "image/png",
      buffer: fs.readFileSync(RESUME),
    });
    await waitForToast(page, "Uploaded e2e-resume.png");
    await expect(page.locator("#docs")).toContainText("e2e-resume.png");
    await attachScreenshot(page, "seeker-files", false);

    await goto(page, "/");
    await waitForBoard(page);
    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().includes("/api/jobs?") &&
          new URL(response.url()).searchParams.get("q") === "Bookkeeper",
      ),
      page.locator("#q").fill("Bookkeeper"),
    ]);
    await page.locator("#jobs .job", { hasText: "Bookkeeper (Part-time)" }).first().click();
    await expect(page.locator("#mTitle")).toContainText("Bookkeeper (Part-time)");
    await page.locator("#mEasy").click();
    await expect(page.locator("#applyForm")).toBeVisible();
    await expect(page.locator('#docList input[name="doc"]:checked')).toHaveCount(1);
    await page.locator("#mSend").click();
    await expect(page.locator(".notice.good")).toContainText("Application sent to Sinag Demo Corp.");
    await page.locator("#mFoot a", { hasText: "Track my applications" }).click();

    await expect(page.locator("#pApps")).toBeVisible();
    await expect(page.locator("#apps")).toContainText("Bookkeeper (Part-time)");
    await expect(page.locator("#apps .status")).toContainText("Submitted");
    await attachScreenshot(page, "seeker-applications", false);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("#pApps")).toBeVisible();
    await expect(page.locator("#apps")).toContainText("Bookkeeper (Part-time)");
    await expect(page.locator("#apps .status")).toContainText("Submitted");
  });

  test("seeker can update a profile and create a job alert", async ({ page }) => {
    await loginUI(page, DEMO.seeker);
    await goto(page, "/seeker.html#profile");
    await expect(page.locator("#pProfile")).toBeVisible();
    const headline = "E2E Customer Support Specialist";
    await page.locator("#pfHeadline").fill(headline);
    await page.locator("#profileForm button[type=submit]").click();
    await waitForToast(page, "Profile saved");
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("#pfHeadline")).toHaveValue(headline);

    await page.locator("#tAlerts").click();
    await expect(page.locator("#pAlerts")).toBeVisible();
    await page.locator("#alKw").fill("Python");
    await page.locator("#alertForm button[type=submit]").click();
    await waitForToast(page, "Alert added");
    await expect(page.locator("#alerts")).toContainText('"Python"');
  });

  test("new seeker signup lands on the profile with a verification banner", async ({ page }) => {
    const stamp = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
    const email = `e2e.seeker.${stamp}@example.test`;
    const name = `E2E Seeker ${stamp}`;

    await goto(page, "/login.html#signup");
    await expect(page.locator("#pSignup")).toBeVisible();
    await page.locator("#suName").fill(name);
    await page.locator("#suEmail").fill(email);
    await page.locator("#suPass").fill("e2e-password-123");
    await page.locator("#suConsent").check();
    await page.locator("#signupForm button[type=submit]").click();

    await page.waitForURL(/\/seeker\.html#profile$/);
    await expect(page.locator("#hello")).toContainText("Hi, E2E");
    await expect(page.locator("#verifyNotice")).toContainText("Confirm your email.");
    await expect(page.locator("#helloSub")).toContainText("Finish your profile");
    const me = await page.evaluate(async () => {
      const response = await fetch("/api/auth/me");
      return response.json();
    });
    expect(me.user.email).toBe(email);
    expect(me.user.email_verified).toBe(false);

    await page.locator("#resendVerify").click();
    await waitForToast(page, /We sent a new link/);
  });
});
