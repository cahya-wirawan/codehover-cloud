import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import { PrivacyPage, SupportPage } from "../legal/LegalPages";
import { UsagePage } from "./UsagePage";

const PLANS = [
  { key: "trial", name: "Trial", allowance_micro_usd: 2_000_000, seats: 1, projects: 1, storage_gib: 1, active_builds: 1, available: true },
  { key: "pro", name: "Pro", allowance_micro_usd: 5_000_000, seats: 1, projects: 10, storage_gib: 5, active_builds: 1, available: true },
  { key: "team", name: "Team", allowance_micro_usd: 20_000_000, seats: 5, projects: 50, storage_gib: 25, active_builds: 2, available: true },
];
const NONE = {
  configured: true,
  enforced: false,
  plan: null,
  status: null,
  period_end: null,
  cancel_at_period_end: false,
  trial_ends_at: null,
  trial_available: true,
  generation_blocked: null,
  hosting_suspended: false,
  has_customer: false,
  plans: PLANS,
};
const USAGE = {
  period_start: "2026-11-01T00:00:00Z",
  period_end: "2026-12-01T00:00:00Z",
  allowance_micro_usd: 5_000_000,
  reserved_micro_usd: 0,
  settled_micro_usd: 1_250_000,
  available_micro_usd: 3_750_000,
};

function serve(billing: () => unknown, role = "owner", sent: { url: string; body: unknown }[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method === "POST") {
        sent.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
        if (url.endsWith("/billing/checkout")) return new Response(JSON.stringify({ url: "http://evil.test/" }));
        if (url.endsWith("/billing/trial")) return new Response(JSON.stringify({ ...NONE, plan: "trial", status: "trial", trial_available: false }));
      }
      if (init?.method === "PUT" && url.endsWith("/billing/anchor")) {
        sent.push({ url, body: null });
        return new Response(JSON.stringify({ ...(billing() as object), holder: { id: "w1", name: "Team" }, holds_billing: true }));
      }
      if (url === "/api/v1/workspaces") return new Response(JSON.stringify({ items: [{ id: "w1", name: "Team", slug: "team", role }], next_cursor: null }));
      if (url === "/api/v1/workspaces/w1/billing") return new Response(JSON.stringify(billing()));
      if (url === "/api/v1/workspaces/w1/usage") return new Response(JSON.stringify(USAGE));
      if (url.startsWith("/api/v1/workspaces/w1/usage/entries")) return new Response(JSON.stringify({ items: [], next_cursor: null }));
      if (url === "/api/v1/meta")
        return new Response(JSON.stringify({ operator_name: "Example Ops", support_email: "help@example.test", billing_available: true, trial_days: 14, plans: PLANS }));
      return new Response("{}", { status: 404 });
    }),
  );
  return sent;
}

function page(path = "/w/w1/usage") {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/w/:wid/usage" element={<UsagePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

test("returning from checkout shows a waiting state, not a plan, until the server confirms it", async () => {
  let confirmed = false;
  serve(() => (confirmed ? { ...NONE, plan: "pro", status: "active", has_customer: true, trial_available: false } : NONE));
  page("/w/w1/usage?checkout=returned");
  expect(await screen.findByText("Confirming your payment")).toBeInTheDocument();
  expect(screen.queryByText("Renews on", { exact: false })).toBeNull();
  confirmed = true;
  expect(await screen.findByText("Pro", { selector: "span" }, { timeout: 5000 })).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByText("Confirming your payment")).toBeNull());
  expect(screen.getByRole("button", { name: "Manage payment and invoices" })).toBeInTheDocument();
}, 10_000);

test("owners choose a plan or start the trial; only https provider pages are followed", async () => {
  const sent = serve(() => NONE);
  const assign = vi.fn();
  vi.stubGlobal("location", { ...window.location, assign });
  page();
  fireEvent.click(await screen.findByRole("button", { name: "Choose Team" }));
  await waitFor(() => expect(sent.map((s) => s.url)).toContain("/api/v1/workspaces/w1/billing/checkout"));
  expect(sent[0]?.body).toEqual({ plan: "team" });
  expect(assign).not.toHaveBeenCalled(); // the fake answered with a non-https URL
  fireEvent.click(screen.getByRole("button", { name: "Start the trial" }));
  await waitFor(() => expect(sent.map((s) => s.url)).toContain("/api/v1/workspaces/w1/billing/trial"));
  expect((await screen.findAllByText("Trial", { selector: "span" })).length).toBeGreaterThan(0);
  expect(screen.getByText("Current plan", { selector: "span" })).toBeInTheDocument();
});

test("members see the plan and usage but no billing actions", async () => {
  serve(() => ({ ...NONE, plan: "pro", status: "past_due", generation_blocked: "past_due", has_customer: true }), "editor");
  page();
  expect(await screen.findByRole("alert")).toHaveTextContent("A payment is overdue");
  expect(screen.getByRole("alert")).toHaveTextContent("Ask the plan's owner");
  expect(screen.queryByRole("button", { name: "Manage payment and invoices" })).toBeNull();
  expect(await screen.findByText("$3.75")).toBeInTheDocument();
  expect(screen.getByRole("meter", { name: "Allowance used" })).toHaveAttribute("aria-valuenow", "25");
});

test("legal and support pages are marked as drafts and name the operator's contact", async () => {
  serve(() => NONE);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <PrivacyPage />
        <SupportPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(screen.getByRole("heading", { name: "Privacy notice" })).toBeInTheDocument();
  expect(screen.getAllByRole("note")[0]).toHaveTextContent("Draft for legal review");
  expect(await screen.findByText(/Example Ops runs this service/)).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "help@example.test" })[0]).toHaveAttribute("href", "mailto:help@example.test");
});

test("a workspace that does not hold the owner's billing can take it over", async () => {
  const sent = serve(() => ({ ...NONE, can_manage: true, owner: { kind: "organization", handle: "acme" }, holder: { id: "w2", name: "Old" }, holds_billing: false }));
  page();
  expect(await screen.findByRole("link", { name: "Old" })).toHaveAttribute("href", "/w/w2/usage");
  fireEvent.click(screen.getByRole("button", { name: "Hold billing here" }));
  expect(await screen.findByText(/This workspace holds the billing of acme/)).toBeInTheDocument();
  expect(sent).toContainEqual({ url: "/api/v1/workspaces/w1/billing/anchor", body: null });
});
