import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { ConnectedAppsPanel } from "../workspaces/AccessTokenPanel";
import { OAuthConsentPage } from "./OAuthConsentPage";

afterEach(() => vi.unstubAllGlobals());

const REQUEST = {
  client_name: "Claude Code",
  redirect_host: "http://127.0.0.1:33418",
  site: "grace/team-site",
  project: "API",
  workspace: "Team",
  visibility: "private",
  scope: "mcp:read",
};

function serve(
  calls: { method: string; url: string; body?: unknown }[],
  status = 200,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf")
        return new Response(JSON.stringify({ csrf_token: "t" }));
      const method = init?.method ?? "GET";
      if (method === "POST") {
        calls.push({ method, url, body: JSON.parse(String(init?.body)) });
        return new Response(
          JSON.stringify({
            redirect_to: "http://127.0.0.1:33418/callback?code=c&state=s",
          }),
        );
      }
      if (method === "DELETE") {
        calls.push({ method, url });
        return new Response(null, { status: 204 });
      }
      if (url.includes("/oauth-grants")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "g1",
                client_name: "Claude Code",
                project_id: "p1",
                project: "API",
                created_at: "2026-11-22T00:00:00Z",
                last_used_at: null,
                revoked_at: null,
              },
            ],
            next_cursor: null,
          }),
        );
      }
      if (status !== 200) {
        return new Response(
          JSON.stringify({
            error: {
              code: "SITE_NOT_FOUND",
              message: "There is no site at this address that you can open.",
              request_id: "r",
              retryable: false,
              details: {},
            },
          }),
          { status },
        );
      }
      return new Response(JSON.stringify(REQUEST));
    }),
  );
}

function page(search: string) {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter initialEntries={[`/oauth/authorize${search}`]}>
        <OAuthConsentPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("the consent page names the app, the site and where it returns, and sends the decision", async () => {
  const calls: { method: string; url: string; body?: unknown }[] = [];
  serve(calls);
  const assign = vi.fn();
  vi.stubGlobal("location", { ...window.location, assign });
  page("?request=r1");
  expect(
    await screen.findByRole("heading", {
      name: "Allow Claude Code to read API?",
    }),
  ).toBeInTheDocument();
  expect(screen.getByText("grace/team-site")).toBeInTheDocument();
  expect(screen.getByText("http://127.0.0.1:33418")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Allow" }));
  await waitFor(() =>
    expect(assign).toHaveBeenCalledWith(
      "http://127.0.0.1:33418/callback?code=c&state=s",
    ),
  );
  expect(calls[0]).toMatchObject({
    url: "/api/v1/oauth/requests/r1/decision",
    body: { approve: true },
  });
});

test("errors from the authorize step and unavailable requests are explained", async () => {
  serve([], 404);
  page("?error=invalid_client");
  expect(await screen.findByRole("alert")).toHaveTextContent("not registered");
});

test("a request for a site the person cannot open shows the server's reason", async () => {
  serve([], 404);
  page("?request=r2");
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "no site at this address",
  );
});

test("connected apps can be disconnected", async () => {
  const calls: { method: string; url: string }[] = [];
  serve(calls);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ConnectedAppsPanel workspaceId="w1" />
    </QueryClientProvider>,
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "Disconnect Claude Code from API",
    }),
  );
  await waitFor(() =>
    expect(calls).toEqual([
      { method: "DELETE", url: "/api/v1/workspaces/w1/oauth-grants/g1" },
    ]),
  );
});
