import {
  attachJson,
  attachScreenshot,
  expect,
  expectNoHorizontalOverflow,
  goto,
  guard,
  test,
  waitForBoard,
} from "./helpers";

test.describe("Public job board", () => {
  test("home page loads live stats, jobs, and the theme control", async ({ page }, testInfo) => {
    await goto(page, "/");
    await waitForBoard(page);

    const cards = page.locator("#jobs .job");
    expect(await cards.count()).toBeGreaterThanOrEqual(10);
    await expect(page.locator("#statUpdated")).not.toHaveText("-");
    await expect(page.locator(".themegroup")).toBeVisible();
    await expect(page.locator(".themegroup button")).toHaveCount(2);
    await expect(page.locator(".themegroup button").first()).toHaveText("Light");
    await expect(page.locator(".themegroup button").last()).toHaveText("Dark");
    await expect(page.locator("#jobs .empty")).toHaveCount(0);

    const titles = await page.locator("#jobs .job h3").allTextContents();
    const companies = await page.locator("#jobs .job .co").allTextContents();
    expect(titles.every((title) => title.trim().length > 0)).toBeTruthy();
    expect(companies.every((company) => company.trim().length > 0)).toBeTruthy();

    const perf = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
      const lcp = performance.getEntriesByType("largest-contentful-paint").at(-1);
      return {
        domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd),
        loadMs: Math.round(nav.loadEventEnd),
        lcpMs: lcp ? Math.round(lcp.startTime) : null,
        jobCards: document.querySelectorAll("#jobs .job").length,
      };
    });
    await attachJson("home-performance", { ...perf, project: testInfo.project.name });
    expect(perf.domContentLoadedMs).toBeLessThan(15_000);

    await attachScreenshot(page, "home-light");
  });

  test("search, empty state, work-setup filter, and reset all work", async ({ page }) => {
    await goto(page, "/");
    await waitForBoard(page);
    const initial = await page.locator("#jobs .job").count();

    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().includes("/api/jobs?") &&
          new URL(response.url()).searchParams.get("q") === "Python",
      ),
      page.locator("#q").fill("Python"),
    ]);
    await expect(page.locator("#resultCount")).not.toHaveText("Loading jobs…");
    expect(await page.locator("#jobs .job").count()).toBeGreaterThan(0);
    expect(
      (await page.locator("#jobs .job").allTextContents()).join(" "),
    ).toMatch(/Python/i);

    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().includes("/api/jobs?") &&
          new URL(response.url()).searchParams.get("q") === "NoSuchJobE2E",
      ),
      page.locator("#q").fill("NoSuchJobE2E"),
    ]);
    await expect(page.locator("#jobs .empty")).toContainText("No jobs match these filters yet.");
    await expect(page.locator("#resultCount")).toHaveText("0 open jobs");

    await Promise.all([
      page.waitForResponse((response) => {
        if (!response.url().includes("/api/jobs?")) return false;
        const params = new URL(response.url()).searchParams;
        return !params.has("q") && !params.has("setup");
      }),
      page.locator("#reset").click(),
    ]);
    await waitForBoard(page);
    expect(await page.locator("#jobs .job").count()).toBe(initial);
    await expect(page.locator("#q")).toHaveValue("");

    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().includes("/api/jobs?") &&
          new URL(response.url()).searchParams.get("setup") === "Remote",
      ),
      page.locator('#fSetup input[value="Remote"]').check(),
    ]);
    const cards = page.locator("#jobs .job");
    await expect(cards.first()).toBeVisible();
    const remoteCards = await cards.count();
    expect(remoteCards).toBeGreaterThan(0);
    await expect(page.locator("#jobs .job .meta span.pill.remote")).toHaveCount(remoteCards);
    await expect(page.locator("#jobs .job .meta span.pill.remote").first()).toHaveText("Remote");
  });

  test("job detail modal opens by keyboard, saves a job, and closes with Escape", async ({ page }) => {
    await goto(page, "/");
    await waitForBoard(page);

    const bookkeeper = page.locator("#jobs .job", { hasText: "Bookkeeper (Part-time)" }).first();
    await expect(bookkeeper).toBeVisible();
    const jobId = Number(await bookkeeper.getAttribute("data-id"));
    await bookkeeper.focus();
    await page.keyboard.press("Enter");

    await expect(page.locator("#scrim")).toBeVisible();
    await expect(page.locator("#mTitle")).toContainText("Bookkeeper (Part-time)");
    await expect(page.locator("#mBody")).toContainText("About the role");
    await expect(page.locator("#mSave")).toHaveText("Save job");
    await expect(page.locator("#mClose")).toBeFocused();

    await page.locator("#mSave").click();
    await expect(page.locator("#mSave")).toHaveText("Saved");
    await page.keyboard.press("Escape");
    await expect(page.locator("#scrim")).toBeHidden();

    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForBoard(page);
    await page.locator("#jobs .job", { hasText: "Bookkeeper (Part-time)" }).first().click();
    await expect(page.locator("#mSave")).toHaveText("Saved");
    await expect
      .poll(() =>
        page.evaluate(() => JSON.parse(localStorage.getItem("sinag-saved") || "[]")),
      )
      .toContain(jobId);
    await page.locator("#mSave").click();
    await expect(page.locator("#mSave")).toHaveText("Save job");
    await page.locator("#mClose").click();
    await expect(page.locator("#scrim")).toBeHidden();
  });

  test("server-rendered job page has the right SEO data", async ({ page, request }) => {
    const response = await request.get("/api/jobs", {
      params: { q: "Bookkeeper", page_size: 5 },
    });
    expect(response.status()).toBe(200);
    const { jobs } = await response.json();
    const job = jobs.find((item: any) => item.title.includes("Bookkeeper"));
    expect(job).toBeTruthy();

    const pageResponse = await page.goto(`/jobs/${job.id}`, {
      waitUntil: "domcontentloaded",
    });
    expect(pageResponse?.status()).toBe(200);
    await expect(page).toHaveURL(new RegExp(`/jobs/${job.id}/bookkeeper-part-time$`));
    await expect(page.locator("h1")).toContainText("Bookkeeper");
    await expect(page.locator('meta[name="robots"]')).toHaveCount(0);

    const structured = await page
      .locator('script[type="application/ld+json"]')
      .allTextContents();
    expect(structured.some((raw) => raw.includes('"@type": "JobPosting"'))).toBeTruthy();
    expect(structured.some((raw) => raw.includes("BaseSalary") || raw.includes("baseSalary"))).toBeTruthy();
    const canonical = await page.locator('link[rel="canonical"]').getAttribute("href");
    expect(canonical).toContain(`/jobs/${job.id}/bookkeeper-part-time`);

    const sitemap = await request.get("/sitemap.xml");
    expect(sitemap.status()).toBe(200);
    expect(await sitemap.text()).toContain(`/jobs/${job.id}/bookkeeper-part-time`);
    const robots = await request.get("/robots.txt");
    expect(robots.status()).toBe(200);
    expect(await robots.text()).toContain("Sitemap:");
  });

  test("light and dark keep all text, images, and layout unchanged", async ({
    page,
    context,
  }, testInfo) => {
    await goto(page, "/");
    await waitForBoard(page);
    await page.evaluate(() => document.fonts.ready);

    const snapshot = () =>
      page.evaluate(() => {
        const box = (selector: string) => {
          const element = document.querySelector(selector);
          if (!element) return null;
          const rect = element.getBoundingClientRect();
          return {
            x: Math.round(rect.x * 10) / 10,
            y: Math.round(rect.y * 10) / 10,
            width: Math.round(rect.width * 10) / 10,
            height: Math.round(rect.height * 10) / 10,
          };
        };
        return {
          text: document.body.innerText.replace(/\s+/g, " ").trim(),
          boxes: {
            header: box("header.top"),
            heading: box(".hero h1"),
            search: box(".search"),
            firstJob: box("#jobs .job"),
            footer: box("footer"),
          },
          images: [...document.images].map((image) => ({
            src: image.currentSrc,
            box: (() => {
              const rect = image.getBoundingClientRect();
              return {
                x: Math.round(rect.x * 10) / 10,
                y: Math.round(rect.y * 10) / 10,
                width: Math.round(rect.width * 10) / 10,
                height: Math.round(rect.height * 10) / 10,
              };
            })(),
          })),
          background: getComputedStyle(document.body).backgroundColor,
          color: getComputedStyle(document.body).color,
        };
      });

    const light = await snapshot();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await attachScreenshot(page, "theme-light", false);

    await page.locator('[data-theme-set="dark"]').click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const dark = await snapshot();
    await attachScreenshot(page, "theme-dark", false);

    expect(dark.text).toBe(light.text);
    expect(dark.images).toEqual(light.images);
    expect(dark.background).not.toBe(light.background);
    expect(dark.color).not.toBe(light.color);
    for (const key of Object.keys(light.boxes) as Array<keyof typeof light.boxes>) {
      expect(dark.boxes[key], `layout changed for ${key}`).toEqual(light.boxes[key]);
    }
    expect(await page.locator("body").innerText()).toContain("Light");
    expect(await page.locator("body").innerText()).toContain("Dark");

    await expect
      .poll(() => page.evaluate(() => localStorage.getItem("sinag_theme")))
      .toBe("dark");
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForBoard(page);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    const second = await context.newPage();
    const secondErrors = await guard(second);
    await goto(second, "/");
    await waitForBoard(second);
    await expect(second.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.locator('[data-theme-set="light"]').click();
    await expect(second.locator("html")).toHaveAttribute("data-theme", "light");
    expect(secondErrors).toEqual([]);

    await attachJson("theme-layout-check", {
      project: testInfo.project.name,
      textEqual: true,
      boxesEqual: true,
      imageCount: light.images.length,
      lightBackground: light.background,
      darkBackground: dark.background,
    });
  });

  test("mobile layout fits and remains usable", async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await goto(page, "/");
    await waitForBoard(page);
    await expect(page.locator("#jobs .job").first()).toBeVisible();
    await expect(page.locator(".themegroup")).toBeVisible();
    await expectNoHorizontalOverflow(page);

    const firstCard = page.locator("#jobs .job").first();
    const cardBox = await firstCard.boundingBox();
    expect(cardBox).not.toBeNull();
    expect(cardBox!.width).toBeLessThanOrEqual(390);

    await attachScreenshot(page, `mobile-home-${testInfo.project.name}`);
  });

  test("public API contract, filter semantics, and write protection", async ({ request }) => {
    const health = await request.get("/api/health");
    expect(health.status()).toBe(200);
    expect(await health.json()).toEqual({ ok: true });

    const pageResponse = await request.get("/api/jobs", {
      params: { page_size: 5 },
    });
    expect(pageResponse.status()).toBe(200);
    const pageBody = await pageResponse.json();
    expect(pageBody.total).toBeGreaterThan(0);
    expect(pageBody.jobs).toHaveLength(5);
    for (const job of pageBody.jobs) {
      expect(job).toEqual(
        expect.objectContaining({
          id: expect.any(Number),
          title: expect.any(String),
          company: expect.objectContaining({ name: expect.any(String) }),
          setup: expect.any(String),
          easy_apply: expect.any(Boolean),
        }),
      );
    }

    const remote = await request.get("/api/jobs", {
      params: { setup: "Remote", page_size: 100 },
    });
    expect(remote.status()).toBe(200);
    const remoteBody = await remote.json();
    expect(remoteBody.jobs.length).toBeGreaterThan(0);
    expect(remoteBody.jobs.every((job: any) => job.setup === "Remote")).toBeTruthy();

    const easy = await request.get("/api/jobs", {
      params: { easy_apply: "true", page_size: 100 },
    });
    expect(easy.status()).toBe(200);
    const easyBody = await easy.json();
    expect(easyBody.jobs.length).toBeGreaterThan(0);
    expect(easyBody.jobs.every((job: any) => job.easy_apply === true)).toBeTruthy();

    const facets = await request.get("/api/facets");
    expect(facets.status()).toBe(200);
    const facetBody = await facets.json();
    expect(facetBody.total_open).toBeGreaterThan(0);
    expect(facetBody.companies).toBeGreaterThan(0);
    expect(Array.isArray(facetBody.setup)).toBeTruthy();
    expect(Array.isArray(facetBody.category)).toBeTruthy();

    const detail = await request.get(`/api/jobs/${easyBody.jobs[0].id}`);
    expect(detail.status()).toBe(200);
    expect((await detail.json()).id).toBe(easyBody.jobs[0].id);

    const missing = await request.get("/api/jobs/99999999");
    expect(missing.status()).toBe(404);
    const admin = await request.get("/api/admin/status");
    expect(admin.status()).toBe(401);

    const crossOrigin = await request.post("/api/auth/login", {
      headers: { Origin: "https://evil.example" },
      data: { email: "seeker@demo.sinag", password: "sinag-demo-123" },
    });
    expect(crossOrigin.status()).toBe(403);

    const home = await request.get("/");
    expect(home.status()).toBe(200);
    expect(home.headers()["x-content-type-options"]).toBe("nosniff");
    expect(home.headers()["referrer-policy"]).toBe("strict-origin-when-cross-origin");
    expect(home.headers()["cache-control"]).toContain("no-cache");

    const sitemap = await request.get("/sitemap.xml");
    expect(sitemap.status()).toBe(200);
    expect(await sitemap.text()).toContain("<urlset");
    const robots = await request.get("/robots.txt");
    expect(robots.status()).toBe(200);
    expect(await robots.text()).toContain("Sitemap:");
  });
});
