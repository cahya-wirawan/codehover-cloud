import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";

import { SupportAccessPanel } from "../workspaces/SupportAccessPanel";
import { OpsPage } from "./OpsPage";

const SUMMARY = {
  queue: { runnable_tasks: 2, outbox_oldest_seconds: 0 },
  builds_last_24h: { ready: 3 },
  tasks_by_stage: {},
  builds_paused: false,
  paused_reason: "",
  paused_since: null,
};

function wrap(node: ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

test("the operations view is a plain not-found page for everyone but operators", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "Not found.", request_id: null, retryable: false, details: {} } }), { status: 404 })));
  wrap(<OpsPage />);
  expect(await screen.findByRole("heading", { name: "Not found" }, { timeout: 5000 })).toBeInTheDocument();
  expect(screen.queryByText(/Pause new builds/)).toBeNull();
});

test("operators pause new builds with a reason", async () => {
  const sent: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (url === "/api/v1/ops/builds-paused") {
        sent.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify({ ...SUMMARY, builds_paused: true, paused_reason: "incident" }));
      }
      if (url === "/api/v1/ops/summary") return new Response(JSON.stringify(SUMMARY));
      if (url === "/api/v1/ops/events") return new Response(JSON.stringify({ items: [], next_cursor: null }));
      return new Response("{}", { status: 404 });
    }),
  );
  wrap(<OpsPage />);
  expect(await screen.findByText("Accepting new builds.", { exact: false })).toBeInTheDocument();
  expect(await screen.findByText("builds ready (24 h)")).toBeInTheDocument();
  fireEvent.change(screen.getAllByRole("textbox", { name: "Reason" })[0] as HTMLElement, { target: { value: "incident" } });
  fireEvent.click(screen.getByRole("button", { name: "Pause new builds" }));
  await waitFor(() => expect(sent).toEqual([{ paused: true, reason: "incident" }]));
});

test("admins see active support access and can end it", async () => {
  const deleted: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method === "DELETE") {
        deleted.push(url);
        return new Response(null, { status: 204 });
      }
      return new Response(
        JSON.stringify({
          items: [{ id: "g1", granted_by_name: "Ada", reason: "stuck build", created_at: "2026-11-18T00:00:00Z", expires_at: "2026-11-18T04:00:00Z", revoked_at: null, active: true }],
          next_cursor: null,
        }),
      );
    }),
  );
  wrap(<SupportAccessPanel workspaceId="w1" />);
  expect(await screen.findByText(/stuck build \(granted by Ada\)/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "End access now" }));
  await waitFor(() => expect(deleted).toEqual(["/api/v1/workspaces/w1/support-access/g1"]));
});

const PERSON = {
  user_id: "u1",
  handle: "ada",
  status: "active",
  created_at: "2026-11-01T00:00:00Z",
  disabled_at: null,
  last_active_at: "2026-11-25T00:00:00Z",
  workspaces: 1,
  organizations: 0,
  builds_this_month: 3,
  spent_this_month_micro_usd: 1_250_000,
  operator: false,
};
const DETAIL = {
  ...PERSON,
  workspaces: [{ workspace_id: "w1", owner_handle: "ada", owner_kind: "user", role: "owner", via_organization: false, builds_this_month: 3, spent_this_month_micro_usd: 1_250_000 }],
  organizations: [],
  sign_in: ["github"],
  live_sessions: 1,
  live_tokens: 2,
  cancelled_builds: 0,
};
const OWNER = {
  namespace_id: "n1",
  handle: "ada",
  kind: "user",
  status: "active",
  workspaces: 1,
  projects: 2,
  published_sites: 1,
  builds_this_month: 3,
  allowance_micro_usd: 20_000_000,
  settled_micro_usd: 1_250_000,
  reserved_micro_usd: 0,
  granted_micro_usd: 0,
  plan_key: null,
};

function operations(sent: { method: string; url: string; body: unknown }[]) {
  let suspended = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (method !== "GET") sent.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url === "/api/v1/ops/summary") return new Response(JSON.stringify(SUMMARY));
      if (url === "/api/v1/ops/events") return new Response(JSON.stringify({ items: [], next_cursor: null }));
      if (url.startsWith("/api/v1/ops/people?")) return new Response(JSON.stringify({ items: [PERSON], next_cursor: null }));
      if (url === "/api/v1/ops/people/lookup") return new Response(JSON.stringify({ items: [], next_cursor: null }));
      if (url === "/api/v1/ops/people/u1/suspension" && method === "POST") {
        suspended = true;
        return new Response(JSON.stringify({ ...DETAIL, status: "disabled", disabled_at: "2026-11-26T00:00:00Z", cancelled_builds: 2, live_sessions: 0, live_tokens: 0 }));
      }
      if (url === "/api/v1/ops/people/u1")
        return new Response(JSON.stringify(suspended ? { ...DETAIL, status: "disabled", disabled_at: "2026-11-26T00:00:00Z" } : DETAIL));
      if (url.startsWith("/api/v1/ops/owners?")) return new Response(JSON.stringify({ items: [OWNER], next_cursor: null }));
      if (url === "/api/v1/ops/owners/n1/credit")
        return new Response(
          JSON.stringify({ id: "g1", workspace_id: "w1", allowance_micro_usd: 1_250_000, starts_at: "2026-11-26T00:00:00Z", expires_at: "2026-12-01T00:00:00Z", reason: "ours", revoked_at: null, kind: "credit" }),
          { status: 201 },
        );
      return new Response("{}", { status: 404 });
    }),
  );
}

test("operators find people by handle or exact email and suspend them with a reason", async () => {
  const sent: { method: string; url: string; body: unknown }[] = [];
  operations(sent);
  wrap(<OpsPage />);
  const people = within(await screen.findByRole("region", { name: "People" }));
  fireEvent.change(people.getByRole("textbox", { name: "Exact email" }), { target: { value: "nobody@example.test" } });
  fireEvent.click(people.getByRole("button", { name: "Find" }));
  expect(await people.findByText("Nobody has that email.")).toBeInTheDocument();
  expect(sent[0]).toEqual({ method: "POST", url: "/api/v1/ops/people/lookup", body: { email: "nobody@example.test" } });
  fireEvent.click(people.getByRole("button", { name: "Back to the list" }));
  fireEvent.click(await people.findByRole("button", { name: "Open ada" }));
  expect(await people.findByText(/Signs in with github/)).toBeInTheDocument();
  fireEvent.change(people.getAllByRole("textbox", { name: "Reason" })[0] as HTMLElement, { target: { value: "abuse report" } });
  fireEvent.click(people.getByRole("button", { name: "Suspend" }));
  expect(await people.findByText("Suspended. 2 running builds cancelled.")).toBeInTheDocument();
  expect(sent[1]).toEqual({ method: "POST", url: "/api/v1/ops/people/u1/suspension", body: { reason: "abuse report" } });
  expect(await people.findByRole("button", { name: "Reinstate" })).toBeInTheDocument();
});

test("operators credit an owner's spending this month", async () => {
  const sent: { method: string; url: string; body: unknown }[] = [];
  operations(sent);
  wrap(<OpsPage />);
  const owners = within(await screen.findByRole("region", { name: "Owners" }));
  fireEvent.click(await owners.findByRole("button", { name: "Credit ada" }));
  fireEvent.change(owners.getByRole("textbox", { name: "Reason for crediting ada" }), { target: { value: "ours" } });
  fireEvent.click(owners.getByRole("button", { name: "Credit this month's spending" }));
  expect(await owners.findByText(/Credited \$1\.25 until/)).toBeInTheDocument();
  expect(sent).toEqual([{ method: "POST", url: "/api/v1/ops/owners/n1/credit", body: { reason: "ours" } }]);
});
