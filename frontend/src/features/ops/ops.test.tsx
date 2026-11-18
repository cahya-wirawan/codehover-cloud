import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  expect(await screen.findByRole("heading", { name: "Not found" })).toBeInTheDocument();
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
