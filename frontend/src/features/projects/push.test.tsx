import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { PushPanel } from "./PushPanel";

const POLICY = { policy: "off", cap_micro_usd: 0, set_by_name: null, version: 3 };
const HISTORY = {
  items: [
    { id: "r1", commit_sha: "c0ffee".padEnd(40, "0"), ref: "main", pushes: 3, state: "done", outcome: "build_started", snapshot_id: null, build_id: "b9", first_received_at: "2026-11-18T10:00:00Z", updated_at: "2026-11-18T10:01:00Z" },
    { id: "r2", commit_sha: "bead".padEnd(40, "0"), ref: "main", pushes: 1, state: "skipped", outcome: "over_push_cap", snapshot_id: null, build_id: null, first_received_at: "2026-11-18T09:00:00Z", updated_at: "2026-11-18T09:01:00Z" },
  ],
  next_cursor: null,
};

function serve(sent: { body: unknown; ifMatch: string | null }[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (url === "/api/v1/projects/p1/push-policy" && init?.method === "PUT") {
        sent.push({ body: JSON.parse(String(init.body)), ifMatch: new Headers(init.headers).get("If-Match") });
        return new Response(JSON.stringify({ policy: "build", cap_micro_usd: 2_500_000, set_by_name: "Ada", version: 4 }));
      }
      if (url === "/api/v1/projects/p1/push-policy")
        return new Response(JSON.stringify(POLICY), { headers: { ETag: '"push-policy-3"' } });
      if (url === "/api/v1/projects/p1/push-requests") return new Response(JSON.stringify(HISTORY));
      return new Response("{}", { status: 404 });
    }),
  );
}

function panel(canChange: boolean) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <PushPanel projectId="p1" trackedRef="main" canChange={canChange} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

test("admins choose what a push does, sending the policy's ETag", async () => {
  const sent: { body: unknown; ifMatch: string | null }[] = [];
  serve(sent);
  panel(true);
  const select = await screen.findByRole("combobox", { name: "On push" });
  expect(screen.getByRole("textbox", { name: "Build cap (USD)" })).toBeDisabled();
  fireEvent.change(select, { target: { value: "build" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Build cap (USD)" }), { target: { value: "2.50" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(sent).toEqual([{ body: { policy: "build", cap_micro_usd: 2_500_000 }, ifMatch: '"push-policy-3"' }]));
  expect(screen.getByText("3 pushes")).toBeInTheDocument();
  expect(screen.getByText("Estimate above the cap: waiting for approval")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open build" })).toHaveAttribute("href", "/p/p1/builds/b9");
});

test("other members see the setting and history but cannot change it", async () => {
  serve([]);
  panel(false);
  expect(await screen.findByRole("combobox", { name: "On push" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  expect(screen.getByText("Only workspace admins can change this.")).toBeInTheDocument();
});
