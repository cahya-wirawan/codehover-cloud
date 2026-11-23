import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, MemoryRouter, Route, Routes, RouterProvider } from "react-router";

import { RequireSignIn } from "../../app/RequireSignIn";
import { routes } from "../../app/routes";
import { WelcomePage } from "../account/WelcomePage";
import { OrganizationPage } from "./OrganizationPage";

afterEach(() => vi.unstubAllGlobals());

type Sent = { method: string; url: string; body: unknown };

function stub(sent: Sent[], me: Record<string, unknown> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method && init.method !== "GET") {
        sent.push({ method: init.method, url, body: init.body ? JSON.parse(String(init.body)) : null });
        if (url === "/api/v1/me/handle") return new Response(JSON.stringify({ handle: "grace" }));
        if (url === "/api/v1/workspaces") return new Response(JSON.stringify({ id: "w9", name: "Docs", slug: "docs", role: "owner" }), { status: 201 });
        if (url.endsWith("/accept")) return new Response(JSON.stringify({ organization_id: "o2" }));
        if (url.endsWith("/invitations")) return new Response(JSON.stringify({ id: "i9", handle: "ada", role: "member", created_at: "2026-11-22T00:00:00Z", expires_at: "2026-12-06T00:00:00Z" }), { status: 201 });
        return new Response(null, { status: 204 });
      }
      if (url === "/api/v1/me")
        return new Response(JSON.stringify({ id: "u1", display_name: "Grace", primary_email: null, operator: false, handle: "grace", handle_confirmed: true, ...me }));
      if (url === "/api/v1/workspaces")
        return new Response(
          JSON.stringify({
            items: [
              { id: "w1", name: "Team", slug: "team", role: "owner", owner: { kind: "organization", handle: "acme", organization_id: "o1" } },
              { id: "w2", name: "Mine", slug: "mine", role: "owner", owner: { kind: "user", handle: "grace", organization_id: null } },
            ],
          }),
        );
      if (url === "/api/v1/organizations")
        return new Response(
          JSON.stringify({
            items: [{ id: "o1", name: "Acme", handle: "acme", role: "admin" }],
            invitations: [{ id: "i1", role: "member", expires_at: "2026-12-06T00:00:00Z", organization: { id: "o2", name: "Globex", handle: "globex" } }],
          }),
        );
      if (url === "/api/v1/organizations/o1") return new Response(JSON.stringify({ id: "o1", name: "Acme", handle: "acme", role: "owner", created_at: "2026-11-22T00:00:00Z" }));
      if (url === "/api/v1/organizations/o1/workspaces") return new Response(JSON.stringify({ items: [{ id: "w1", name: "Team", slug: "team", role: "owner" }] }));
      if (url === "/api/v1/organizations/o1/members")
        return new Response(
          JSON.stringify({ items: [
            { user_id: "u1", display_name: "Grace", handle: "grace", role: "owner", created_at: "2026-11-22T00:00:00Z" },
            { user_id: "u2", display_name: "Ada", handle: "ada", role: "member", created_at: "2026-11-22T00:00:00Z" },
          ] }),
        );
      if (url === "/api/v1/organizations/o1/invitations") return new Response(JSON.stringify({ items: [] }));
      return new Response("{}", { status: 404 });
    }),
  );
}

function at(path: string, routes: { path: string; element: React.ReactNode }[]) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<RequireSignIn />}>
            {routes.map((r) => (
              <Route key={r.path} path={r.path} element={r.element} />
            ))}
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("a person who has not confirmed their name is asked for it once, then continues", async () => {
  const sent: Sent[] = [];
  stub(sent, { handle: "grace-h", handle_confirmed: false });
  at("/w", [
    { path: "/w", element: <p>workspaces here</p> },
    { path: "/welcome", element: <WelcomePage /> },
  ]);
  const field = await screen.findByLabelText("Your name");
  expect(field).toHaveValue("grace-h");
  fireEvent.change(field, { target: { value: "Grace" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await waitFor(() => expect(sent).toEqual([{ method: "PUT", url: "/api/v1/me/handle", body: { handle: "grace" } }]));
});

test("your own workspaces page lists your workspaces and invitations; accepting one switches to it", async () => {
  const sent: Sent[] = [];
  stub(sent);
  const router = createMemoryRouter(routes, { initialEntries: ["/w?owner=grace"] });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("heading", { name: "Your workspaces" })).toBeInTheDocument();
  expect(await screen.findByText("mine")).toBeInTheDocument();
  expect(screen.queryByText("team")).toBeNull(); // the organization's workspace is not yours
  fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
  await waitFor(() => expect(sent.map((s) => s.url)).toContain("/api/v1/organization-invitations/i1/accept"));
  await waitFor(() => expect(router.state.location.search).toBe("?owner=globex"));
});

test("an organization's workspaces page lists its workspaces and creates new ones in it", async () => {
  const sent: Sent[] = [];
  stub(sent);
  const router = createMemoryRouter(routes, { initialEntries: ["/w?owner=acme"] });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();
  expect(await screen.findByText("team")).toBeInTheDocument();
  expect(screen.queryByText("mine")).toBeNull();
  expect(screen.getByRole("link", { name: "Organization settings" })).toHaveAttribute("href", "/o/o1");
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Docs" } });
  fireEvent.change(screen.getByLabelText("Address"), { target: { value: "docs" } });
  fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));
  await waitFor(() =>
    expect(sent).toContainEqual({ method: "POST", url: "/api/v1/workspaces", body: { name: "Docs", slug: "docs", organization_id: "o1" } }),
  );
});

test("the sidebar switches between you and your organizations", async () => {
  const sent: Sent[] = [];
  stub(sent);
  const router = createMemoryRouter(routes, { initialEntries: ["/w?owner=grace"] });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  const [switcher] = await screen.findAllByRole("button", { name: /Switch between you and your organizations, 1 open invitation/ });
  if (!switcher) throw new Error("no switcher");
  fireEvent.pointerDown(switcher, { button: 0, ctrlKey: false });
  fireEvent.click(await screen.findByRole("menuitem", { name: /Acme/ }));
  await waitFor(() => expect(router.state.location.search).toBe("?owner=acme"));
  expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();
});

test("an organization owner manages roles and invites by name", async () => {
  const sent: Sent[] = [];
  stub(sent);
  at("/o/o1", [{ path: "/o/:oid", element: <OrganizationPage /> }]);
  expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Team/ })).toHaveAttribute("href", "/w/w1");
  fireEvent.change(await screen.findByLabelText("Role of ada"), { target: { value: "admin" } });
  await waitFor(() => expect(sent).toContainEqual({ method: "PATCH", url: "/api/v1/organizations/o1/members/u2", body: { role: "admin" } }));
  fireEvent.change(screen.getByLabelText("Their name"), { target: { value: "Katherine" } });
  fireEvent.click(screen.getByRole("button", { name: "Invite" }));
  await waitFor(() =>
    expect(sent).toContainEqual({ method: "POST", url: "/api/v1/organizations/o1/invitations", body: { handle: "katherine", role: "member" } }),
  );
});
