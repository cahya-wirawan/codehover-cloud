import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { resetCsrfToken } from "../lib/api/client";
import { routes } from "./routes";

type Reply = { status: number; body: unknown };

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

/** A tiny fake API: replies by "METHOD path"; unknown routes are 404 envelopes. */
function fakeApi(replies: Record<string, Reply>) {
  const calls: { key: string; init: RequestInit | undefined }[] = [];
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const key = `${init?.method ?? "GET"} ${url}`;
    calls.push({ key, init });
    const reply = replies[key] ?? {
      status: 404,
      body: { error: { code: "NOT_FOUND", message: "Not found", request_id: null, retryable: false, details: {} } },
    };
    return new Response(reply.status === 204 ? null : JSON.stringify(reply.body), { status: reply.status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const LIVE: Reply = { status: 200, body: { status: "ok", version: "0.1.0" } };
const SIGNED_OUT: Reply = {
  status: 401,
  body: { error: { code: "UNAUTHENTICATED", message: "Sign in", request_id: null, retryable: false, details: {} } },
};
const GRACE: Reply = { status: 200, body: { id: "u1", display_name: "Grace Hopper", primary_email: null, identities: [] } };

afterEach(() => {
  vi.unstubAllGlobals();
  resetCsrfToken();
});

test("home page shows API version from the live endpoint", async () => {
  const calls = fakeApi({ "GET /api/v1/health/live": LIVE, "GET /api/v1/me": SIGNED_OUT });
  renderAt("/");
  expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  expect(await screen.findByText(/API online/)).toHaveTextContent("version 0.1.0");
  const live = calls.find((c) => c.key === "GET /api/v1/health/live");
  expect(live?.init?.credentials).toBe("same-origin");
});

test("API error shows an alert with the request reference", async () => {
  fakeApi({
    "GET /api/v1/health/live": {
      status: 503,
      body: { error: { code: "NOT_READY", message: "Not ready", request_id: "req-1234abcd", retryable: true, details: {} } },
    },
    "GET /api/v1/me": SIGNED_OUT,
  });
  renderAt("/");
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("API unavailable");
  expect(alert).toHaveTextContent("req-1234abcd");
});

test("unknown routes render the not-found page inside the shell", () => {
  fakeApi({ "GET /api/v1/health/live": LIVE, "GET /api/v1/me": SIGNED_OUT });
  renderAt("/does/not/exist");
  expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "codehover-cloud" })).toBeInTheDocument();
});

test("signed-out visitors see a sign-in link", async () => {
  fakeApi({ "GET /api/v1/health/live": LIVE, "GET /api/v1/me": SIGNED_OUT });
  renderAt("/");
  expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
});

test("login page offers both providers with a safe return path and explains errors", () => {
  fakeApi({ "GET /api/v1/me": SIGNED_OUT });
  renderAt("/login?error=expired&return_to=//evil.example");
  expect(screen.getByRole("alert")).toHaveTextContent("expired or was already used");
  expect(screen.getByRole("link", { name: "Continue with Google" })).toHaveAttribute(
    "href",
    "/api/v1/auth/google/start?return_to=%2F",
  );
  expect(screen.getByRole("link", { name: "Continue with GitHub" })).toHaveAttribute(
    "href",
    "/api/v1/auth/github/start?return_to=%2F",
  );
});

test("sign out sends the CSRF token and returns to the login page", async () => {
  const calls = fakeApi({
    "GET /api/v1/health/live": LIVE,
    "GET /api/v1/me": GRACE,
    "GET /api/v1/auth/csrf": { status: 200, body: { csrf_token: "csrf-abc" } },
    "POST /api/v1/auth/logout": { status: 204, body: null },
  });
  const router = renderAt("/");
  fireEvent.keyDown(await screen.findByRole("button", { name: /Account menu/ }), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  const logout = calls.find((c) => c.key === "POST /api/v1/auth/logout");
  expect(new Headers(logout?.init?.headers).get("X-CSRF-Token")).toBe("csrf-abc");
});
