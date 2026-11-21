import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { extname, join, normalize } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { type Browser, expect, type Page, test } from "@playwright/test";

/** WCAG 2.1 A/AA: serious and critical findings fail the journey (02 "Shared states", 17). */
async function accessible(page: Page, screen: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const blocking = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${screen}: ${v.id} at ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
  expect(blocking).toEqual([]);
}

/** Sign in through the fake Google provider: the redirect to accounts.google.com is answered locally. */
async function signIn(page: Page, baseURL: string) {
  // Playwright does not route requests that are redirect targets, so the app's own start redirect is
  // rewritten: the browser never contacts the real provider. Anything else outside the app aborts.
  await page.route("**/api/v1/auth/google/start*", async (route) => {
    const response = await route.fetch({ maxRedirects: 0 });
    const query = new URL(response.headers()["location"] ?? "").search;
    await route.fulfill({ status: 302, headers: { ...response.headers(), location: `${baseURL}/__e2e__/google${query}` } });
  });
  await page.context().route((url) => !url.href.startsWith(baseURL), (route) => route.abort());
  await page.goto("/login");
  await accessible(page, "login");
  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(page.getByRole("heading", { name: /Understand any repository/ })).toBeVisible();
}

/** Another person: a fresh browser context signed in as a new user (each fake sign-in is a new subject). */
async function personPage(browser: Browser, baseURL: string): Promise<Page> {
  const page = await (await browser.newContext({ baseURL })).newPage();
  await signIn(page, baseURL);
  return page;
}

async function anonymousPage(browser: Browser, baseURL: string): Promise<Page> {
  const context = await browser.newContext({ baseURL });
  await context.route((url) => !url.href.startsWith(baseURL), (route) => route.abort());
  return context.newPage();
}

test("sign in, build, read, review, publish to members then the public, withdraw, export, read offline and delete", async ({ page, browser, baseURL }) => {
  const base = baseURL ?? "";
  const run = Math.random().toString(36).slice(2, 8);
  await signIn(page, base);

  // Workspace, GitHub connection (seeded: the connect flow has its own API tests), project.
  await page.goto("/w");
  await page.getByLabel("Name", { exact: true }).fill("E2E Team");
  await page.getByLabel("Address", { exact: true }).fill(`e2e-${run}`);
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByRole("link", { name: /E2E Team/ })).toBeVisible();
  const workspaces = (await (await page.request.get("/api/v1/workspaces")).json()) as { items: { id: string; slug: string }[] };
  const wid = workspaces.items.find((w) => w.slug === `e2e-${run}`)?.id ?? "";
  expect(wid).not.toBe("");
  expect((await page.request.post("/__e2e__/seed-github", { data: { workspace_id: wid } })).ok()).toBe(true);
  await page.goto(`/w/${wid}`);
  await page.getByLabel("Connection").selectOption({ label: "octo-org" });
  await page.getByRole("combobox", { name: "Repository" }).selectOption({ label: "octo-org/api" });
  await page.getByLabel("Name", { exact: true }).fill("API");
  await page.getByLabel("Address", { exact: true }).fill("api");
  await page.getByRole("button", { name: "Create project" }).click();
  await accessible(page, "workspace");
  await page.getByRole("link", { name: "API", exact: true }).click();

  // Snapshot with the exclusion summary, estimate, capped build (free replay preset).
  await page.getByRole("button", { name: "Take snapshot" }).click();
  await expect(page.getByText(/scanned · 3 source files selectable/)).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Estimate" }).click();
  await expect(page.getByText("Cost range")).toBeVisible();
  await accessible(page, "project with estimate");
  await page.getByRole("button", { name: "Start build" }).click();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible({ timeout: 90_000 }); // followed over SSE
  const buildUrl = page.url();
  await accessible(page, "build overview");

  // Read the code: pin a definition, follow it with the keyboard back out.
  await page.getByRole("navigation", { name: "Build sections" }).getByRole("link", { name: "Code" }).click();
  await page.getByRole("button", { name: "src", exact: false }).first().click();
  await page.getByRole("button", { name: "app.py" }).click();
  await page.getByRole("button", { name: "Explain function main" }).click();
  const card = page.getByRole("dialog", { name: "function main" });
  await expect(card).toContainText("Entry point.");
  await accessible(page, "code with pinned card");
  await page.keyboard.press("Escape");
  await expect(card).toBeHidden();

  // Guide with citations into the code, then the review gate.
  await page.getByRole("navigation", { name: "Build sections" }).getByRole("link", { name: "Guide" }).click();
  await page.locator("section[aria-labelledby=guide-title] ul button").first().click();
  await expect(page.getByRole("heading", { name: "Claims" })).toBeVisible();
  await accessible(page, "guide page");
  await page.getByRole("navigation", { name: "Build sections" }).getByRole("link", { name: "Review" }).click();
  await expect(page.getByText("Ready to publish.")).toBeVisible();
  await accessible(page, "review");

  // Publish privately (the default): an invited member reads it; another tenant and anonymous readers
  // get the same "not found" as for a site that does not exist (09, 13).
  const projectUrl = buildUrl.replace(/\/builds\/.*$/, "");
  await page.goto(projectUrl);
  const slug = `e2e-site-${run}`;
  await page.getByRole("textbox", { name: /Site address/ }).fill(slug);
  await page.getByRole("button", { name: "Publish" }).click();
  await expect(page.getByText(/private to this workspace/)).toBeVisible();
  await accessible(page, "private publication");

  const member = await personPage(browser, base);
  const email = ((await (await member.request.get("/api/v1/me")).json()) as { primary_email: string }).primary_email;
  // Invite through the Members page (S17); the link is shown once.
  await page.goto(`/w/${wid}/members`);
  await page.getByRole("textbox", { name: "Email" }).fill(email);
  await page.getByRole("button", { name: "Create link" }).click();
  const acceptUrl = await page.getByRole("textbox", { name: "Invitation link" }).inputValue();
  await expect(page.getByText(email, { exact: true }).last()).toBeVisible();
  await accessible(page, "members");
  const invitation = new URL(acceptUrl);
  await member.goto(invitation.pathname + invitation.hash); // the token stays in the fragment
  await expect(member).toHaveURL(new RegExp(`/w/${wid}$`));
  await member.goto(`/sites/${slug}`);
  await expect(member.getByRole("heading", { name: "API" })).toBeVisible();
  await expect(member.getByText(/private to its workspace/)).toBeVisible();
  await accessible(member, "members-only site");
  await page.goto(projectUrl);

  const stranger = await personPage(browser, base);
  await stranger.goto(`/sites/${slug}`);
  await expect(stranger.getByRole("heading", { name: "Site not found" })).toBeVisible();
  expect((await stranger.request.get(`/api/v1/builds/${buildUrl.split("/builds/")[1]}`)).status()).toBe(404);
  const visitor = await anonymousPage(browser, base);
  await visitor.goto(`/sites/${slug}`);
  await expect(visitor.getByRole("heading", { name: "Site not found" })).toBeVisible();

  // Opt in to public with the typed confirmation; the anonymous reader now sees it.
  await page.getByRole("radio", { name: /public/ }).check();
  await page.getByRole("textbox", { name: /Site address/ }).fill(slug);
  await page.getByRole("region", { name: "Publication" }).getByRole("textbox", { name: /Type the project name/ }).fill("API");
  await page.getByRole("button", { name: "Update publication" }).click();
  await expect(page.getByRole("link", { name: `/sites/${slug}` })).toBeVisible();
  await expect(page.getByText("public", { exact: true })).toBeVisible();
  await accessible(page, "public publication");
  await visitor.reload();
  await expect(visitor.getByRole("heading", { name: "API" })).toBeVisible();
  await expect(visitor.getByText("public", { exact: true })).toBeVisible();
  await accessible(visitor, "public site");
  expect((await visitor.request.get(`/api/v1/builds/${buildUrl.split("/builds/")[1]}`)).status()).toBe(401);

  // Withdraw: the site stops at once.
  await page.getByRole("button", { name: "Stop publishing…" }).click();
  await page.getByRole("button", { name: "Stop publishing API" }).click();
  await expect(page.getByText(/Not published/)).toBeVisible();
  await visitor.reload();
  await expect(visitor.getByRole("heading", { name: "Site not found" })).toBeVisible();

  // Export the offline site and read it with every network request blocked (13 "export", A19).
  await page.goto(buildUrl);
  await page.getByRole("button", { name: "Offline site (ZIP)" }).click();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("link", { name: "Download" }).click({ timeout: 60_000 }),
  ]);
  const directory = mkdtempSync(join(tmpdir(), "codehover-export-"));
  const archive = join(directory, "site.zip");
  await download.saveAs(archive);
  execFileSync("python3", ["-m", "zipfile", "-e", archive, join(directory, "site")]);
  const root = join(directory, "site");
  const offline = await browser.newContext();
  const blocked: string[] = [];
  const types: Record<string, string> = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain" };
  await offline.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "offline.test") {
      blocked.push(url.href);
      return route.abort();
    }
    const file = normalize(join(root, url.pathname));
    if (!file.startsWith(root)) return route.abort();
    return route.fulfill({ body: readFileSync(file), contentType: types[extname(file)] ?? "application/octet-stream" });
  });
  const reader = await offline.newPage();
  await reader.goto("http://offline.test/index.html");
  await expect(reader.getByRole("heading", { name: "API", level: 1 })).toBeVisible();
  await expect(reader.getByRole("navigation", { name: "Guide pages" })).toBeVisible(); // the app's own reading view
  await reader.getByRole("navigation", { name: "Sections" }).getByRole("link", { name: "Code" }).click();
  await reader.getByRole("button", { name: "src", exact: true }).click();
  await reader.getByRole("button", { name: "app.py" }).click();
  await reader.getByRole("button", { name: "Explain function main", exact: true }).click();
  const offlineCard = reader.getByRole("dialog", { name: "function main" }); // floats at the name, pinned
  await expect(offlineCard).toContainText("Entry point.");
  expect(blocked).toEqual([]); // the export loads nothing from the network
  await accessible(reader, "offline export");

  // Delete the project: delivery stops at once for members, and the project is gone (A22).
  await page.goto(projectUrl);
  await page.getByRole("textbox", { name: /Site address/ }).fill(slug);
  await page.getByRole("button", { name: "Publish" }).click();
  await expect(page.getByText(/private to this workspace/)).toBeVisible();
  await member.goto(`/sites/${slug}`);
  await expect(member.getByRole("heading", { name: "API" })).toBeVisible();
  await page.getByRole("region", { name: "Delete project" }).getByRole("textbox").fill("API");
  await accessible(page, "project deletion");
  await page.getByRole("button", { name: "Delete API" }).click();
  await expect(page).toHaveURL(new RegExp(`/w/${wid}$`));
  await expect(page.getByRole("link", { name: "API", exact: true })).toHaveCount(0);
  await member.reload();
  await expect(member.getByRole("heading", { name: "Site not found" })).toBeVisible();
  const projectId = projectUrl.split("/p/")[1] ?? "";
  expect((await page.request.get(`/api/v1/projects/${projectId}`)).status()).toBe(404);
  expect((await page.request.get(`/api/v1/builds/${buildUrl.split("/builds/")[1]}`)).status()).toBe(404);
});

test("plan and usage: the trial, then the privacy, terms and support pages", async ({ page, baseURL }) => {
  const base = baseURL ?? "";
  const run = Math.random().toString(36).slice(2, 8);
  await signIn(page, base);
  await expect(page.getByRole("heading", { name: "Plans" })).toBeVisible();
  await accessible(page, "start page with plans");
  await page.goto("/w");
  await page.getByLabel("Name", { exact: true }).fill("Plan Team");
  await page.getByLabel("Address", { exact: true }).fill(`plan-${run}`);
  await page.getByRole("button", { name: "Create workspace" }).click();
  await page.getByRole("link", { name: /Plan Team/ }).click();
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("link", { name: "Plan and usage" }).click();
  await expect(page.getByRole("heading", { name: "Plan and usage" })).toBeVisible();
  await expect(page.getByText("Paid plans are not offered on this deployment yet.")).toBeVisible();
  await accessible(page, "plan and usage");
  await page.getByRole("button", { name: "Start the trial" }).click();
  await expect(page.getByText(/The trial ends on/)).toBeVisible();
  await accessible(page, "plan and usage on the trial");

  for (const [link, heading] of [["Privacy", "Privacy notice"], ["Terms", "Terms of service"], ["Support", "Support"]] as const) {
    await page.getByRole("navigation", { name: "About this service" }).getByRole("link", { name: link }).click();
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await accessible(page, heading);
  }
});
