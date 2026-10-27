import fs from "node:fs";
import path from "node:path";
import {
  expect,
  test as base,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

export { expect };

export const DEMO = {
  seeker: "seeker@demo.sinag",
  employer: "employer@demo.sinag",
  admin: "admin@demo.sinag",
  password: "sinag-demo-123",
};

export const BASE_URL =
  process.env.E2E_BASE_URL ||
  `http://127.0.0.1:${process.env.E2E_PORT || 8010}`;

const RESUME_FIXTURE = path.resolve(__dirname, "..", "..", "static", "icon-192.png");

// Console/page errors fail a test unless the test explicitly opts out. Every
// browser page created by a test should be passed through prepare() or guard().
export const test = base.extend<{ browserErrors: string[] }>({
  browserErrors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(`console: ${message.text()}`);
      });
      await use(errors);
      expect(errors, "Unexpected browser console or page errors").toEqual([]);
    },
    { auto: true },
  ],
});

const prepared = new WeakSet<Page>();

export async function prepare(page: Page): Promise<void> {
  if (prepared.has(page)) return;
  prepared.add(page);
  // The pages load Google Fonts. Stub the stylesheet so a test run does not
  // depend on fonts.googleapis.com; this also keeps the browser console quiet.
  await page.route("https://fonts.googleapis.com/**", (route) =>
    route.fulfill({ status: 200, contentType: "text/css", body: "" }),
  );
}

export async function guard(page: Page): Promise<string[]> {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  await prepare(page);
  return errors;
}

export async function goto(page: Page, url: string): Promise<void> {
  await prepare(page);
  await page.goto(url, { waitUntil: "domcontentloaded" });
}

export async function waitForBoard(page: Page): Promise<void> {
  await expect(page.locator("#jobs .job").first()).toBeVisible();
  await expect(page.locator("#statJobs")).toHaveText(/^[\d,]+$/);
  await expect(page.locator("#statCompanies")).toHaveText(/^[\d,]+$/);
  await expect(page.locator("#resultCount")).toContainText(/open jobs?/);
}

export async function loginUI(
  page: Page,
  email: string,
  password = DEMO.password,
): Promise<void> {
  await goto(page, "/login.html");
  await page.locator("#siEmail").fill(email);
  await page.locator("#siPass").fill(password);
  await page.locator("#signinForm button[type=submit]").click();
  await page.waitForURL((url) => url.pathname !== "/login.html", { timeout: 20_000 });
}

export async function apiLogin(
  request: APIRequestContext,
  email: string,
  password = DEMO.password,
): Promise<Record<string, unknown>> {
  const response = await request.post("/api/auth/login", {
    data: { email, password },
  });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

export async function findJob(
  request: APIRequestContext,
  query: string,
): Promise<Record<string, any>> {
  const response = await request.get("/api/jobs", {
    params: { q: query, page_size: 25 },
  });
  expect(response.status(), await response.text()).toBe(200);
  const body = await response.json();
  expect(body.jobs.length, `No jobs matched "${query}"`).toBeGreaterThan(0);
  return body.jobs[0];
}

export async function ensureResume(
  request: APIRequestContext,
): Promise<{ id: number; filename: string }> {
  const existing = await request.get("/api/me/documents");
  expect(existing.status(), await existing.text()).toBe(200);
  const docs = await existing.json();
  const resume = docs.find((doc: any) => doc.kind === "resume");
  if (resume) return resume;

  const upload = await request.post("/api/me/documents", {
    multipart: {
      kind: "resume",
      file: {
        name: "e2e-resume.png",
        mimeType: "image/png",
        buffer: fs.readFileSync(RESUME_FIXTURE),
      },
    },
  });
  expect(upload.status(), await upload.text()).toBe(201);
  return upload.json();
}

export async function ensureSeekerApplication(
  request: APIRequestContext,
): Promise<{ job: Record<string, any>; applicationId?: number }> {
  await apiLogin(request, DEMO.seeker);
  const resume = await ensureResume(request);
  const job = await findJob(request, "Bookkeeper");
  const apply = await request.post(`/api/jobs/${job.id}/apply`, {
    data: {
      document_ids: [resume.id],
      cover_letter: "Automated end-to-end test application.",
      answers: {},
      priority: false,
    },
  });
  expect([201, 409], await apply.text()).toContain(apply.status());
  if (apply.status() === 201) {
    const body = await apply.json();
    return { job, applicationId: body.application_id };
  }
  return { job };
}

export function uniqueEmployer(): {
  name: string;
  email: string;
  password: string;
  companyName: string;
} {
  const stamp = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  return {
    name: `E2E Employer ${stamp}`,
    email: `e2e.employer.${stamp}@example.test`,
    password: "e2e-password-123",
    companyName: `E2E Company ${stamp}`,
  };
}

export async function createPendingEmployer(
  request: APIRequestContext,
): Promise<{
  name: string;
  email: string;
  password: string;
  companyName: string;
  company: Record<string, any>;
}> {
  const account = uniqueEmployer();
  const signup = await request.post("/api/auth/signup", {
    data: {
      name: account.name,
      email: account.email,
      password: account.password,
      role: "employer",
      consent: true,
    },
  });
  expect(signup.status(), await signup.text()).toBe(201);

  const company = await request.post("/api/employer/company", {
    data: {
      name: account.companyName,
      industry: "Software & Technology",
      website: "https://example.com/e2e",
      location_text: "Cebu City",
      about: "A temporary company created by the end-to-end test suite.",
    },
  });
  expect(company.status(), await company.text()).toBe(201);
  return { ...account, company: await company.json() };
}

export async function waitForToast(page: Page, text: string | RegExp): Promise<void> {
  await expect(page.locator("#toast")).toContainText(text);
}

export async function attachScreenshot(
  page: Page,
  name: string,
  fullPage = true,
): Promise<void> {
  await test.info().attach(name, {
    body: await page.screenshot({ fullPage, animations: "disabled" }),
    contentType: "image/png",
  });
}

export async function attachJson(name: string, value: unknown): Promise<void> {
  await test.info().attach(name, {
    body: Buffer.from(JSON.stringify(value, null, 2)),
    contentType: "application/json",
  });
}

export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const result = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(
    result.scrollWidth,
    `Page is ${result.scrollWidth}px wide in a ${result.innerWidth}px viewport`,
  ).toBeLessThanOrEqual(result.innerWidth + 1);
}
