import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AccessTokenPanel } from "./AccessTokenPanel";

afterEach(() => vi.unstubAllGlobals());

const LIVE = {
  id: "t1",
  name: "Laptop",
  prefix: "chm_abcd1234",
  scope: "mcp:read",
  created_at: "2026-11-19T00:00:00Z",
  expires_at: "2099-01-01T00:00:00Z",
  last_used_at: null,
  revoked_at: null,
};

function serve(calls: { method: string; url: string; body?: unknown; key?: string | null }[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      const method = init?.method ?? "GET";
      if (method === "POST") {
        const headers = new Headers(init?.headers);
        calls.push({ method, url, body: JSON.parse(String(init?.body)), key: headers.get("Idempotency-Key") });
        return new Response(JSON.stringify({ ...LIVE, id: "t2", name: "CI", token: "chm_secret-value" }), { status: 201 });
      }
      if (method === "DELETE") {
        calls.push({ method, url });
        return new Response(null, { status: 204 });
      }
      return new Response(JSON.stringify({ items: [LIVE], next_cursor: null }));
    }),
  );
}

function panel() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AccessTokenPanel workspaceId="w1" />
    </QueryClientProvider>,
  );
}

test("a new token is shown once, and tokens are listed by prefix only", async () => {
  const calls: { method: string; url: string; body?: unknown; key?: string | null }[] = [];
  serve(calls);
  panel();
  expect(await screen.findByText("chm_abcd1234…")).toBeInTheDocument();
  expect(screen.getByText(/never used/)).toBeInTheDocument();
  const create = screen.getByRole("button", { name: "Create token" });
  expect(create).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Token name"), { target: { value: "CI" } });
  fireEvent.change(screen.getByLabelText("Expires after"), { target: { value: "30" } });
  fireEvent.click(create);
  expect(await screen.findByText("chm_secret-value")).toBeInTheDocument();
  expect(calls[0]).toMatchObject({ url: "/api/v1/workspaces/w1/access-tokens", body: { name: "CI", expires_in_days: 30 } });
  expect(calls[0]?.key).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByText("chm_secret-value")).toBeNull();
});

test("a token can be revoked", async () => {
  const calls: { method: string; url: string }[] = [];
  serve(calls);
  panel();
  fireEvent.click(await screen.findByRole("button", { name: "Revoke token Laptop" }));
  await waitFor(() => expect(calls).toEqual([{ method: "DELETE", url: "/api/v1/workspaces/w1/access-tokens/t1" }]));
});
