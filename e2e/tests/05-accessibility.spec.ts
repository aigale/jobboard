import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import {
  DEMO,
  attachJson,
  expect,
  goto,
  loginUI,
  test,
  waitForBoard,
} from "./helpers";

type AxeSummary = {
  id: string;
  impact: string | null;
  help: string;
  nodes: number;
  targets: string[];
};

async function audit(page: Page, name: string): Promise<AxeSummary[]> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const summary: AxeSummary[] = results.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact ?? null,
    help: violation.help,
    nodes: violation.nodes.length,
    targets: violation.nodes.flatMap((node) => node.target.map(String)).slice(0, 8),
  }));
  await test.info().attach(`axe-${name}`, {
    body: Buffer.from(JSON.stringify({ name, summary, violations: results.violations }, null, 2)),
    contentType: "application/json",
  });
  console.log(
    `A11Y ${name}: ${summary.length} rule(s), ` +
      summary.map((item) => `${item.impact || "unknown"}:${item.id}(${item.nodes})`).join(", "),
  );
  return summary;
}

test.describe("Accessibility audit (informational)", () => {
  test("public home in light and dark", async ({ page }) => {
    await goto(page, "/");
    await waitForBoard(page);
    await expect(page.locator("main")).toBeVisible();
    const light = await audit(page, "home-light");

    await page.locator('[data-theme-set="dark"]').click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const dark = await audit(page, "home-dark");

    await attachJson("a11y-home-summary", { light, dark });
  });

  test("login and seeker dashboard", async ({ page }) => {
    await goto(page, "/login.html");
    await expect(page.locator("main")).toBeVisible();
    await audit(page, "login");

    await loginUI(page, DEMO.seeker);
    await expect(page.locator("#pApps")).toBeVisible();
    await audit(page, "seeker-applications");
  });

  test("employer dashboard", async ({ page }) => {
    await loginUI(page, DEMO.employer);
    await expect(page.locator("#pJobs")).toBeVisible();
    await audit(page, "employer-jobs");
  });

  test("admin dashboard", async ({ page }) => {
    await loginUI(page, DEMO.admin);
    await expect(page.locator("#tiles")).toBeVisible();
    await audit(page, "admin-overview");
  });
});
