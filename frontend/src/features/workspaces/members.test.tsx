import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import { AcceptInvitationPage } from "./AcceptInvitationPage";
import { MembersPage } from "./MembersPage";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

type Sent = { method: string; url: string; body: unknown };

function stubApi(role: "admin" | "owner", sent: Sent[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method && init.method !== "GET") {
        sent.push({ method: init.method, url, body: init.body ? JSON.parse(String(init.body)) : null });
        if (url.endsWith("/invitations"))
          return new Response(
            JSON.stringify({ id: "i1", email: "alan@demo.test", role: "editor", expires_at: "2026-11-26T00:00:00Z", accept_url: "http://x/invitations/accept#token=abc" }),
            { status: 201 },
          );
        return new Response(null, { status: 204 });
      }
      if (url === "/api/v1/me") return new Response(JSON.stringify({ id: "u1", display_name: "Grace", primary_email: null, operator: false }));
      if (url === "/api/v1/workspaces") return new Response(JSON.stringify({ items: [{ id: "w1", name: "Team", slug: "team", role }] }));
      if (url.startsWith("/api/v1/workspaces/w1/members"))
        return new Response(JSON.stringify({ items: [
          { user_id: "u1", display_name: "Grace", role, created_at: "2026-11-19T00:00:00Z" },
          { user_id: "u2", display_name: "Ada", role: "viewer", created_at: "2026-11-19T00:00:00Z" },
          { user_id: "u3", display_name: "Katherine", role: "admin", created_at: "2026-11-19T00:00:00Z" },
        ], next_cursor: null }));
      if (url === "/api/v1/workspaces/w1/invitations") return new Response(JSON.stringify({ items: [] }));
      return new Response("{}", { status: 404 });
    }),
  );
}

function renderMembers() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/w/w1/members"]}>
        <Routes>
          <Route path="/w/:wid/members" element={<MembersPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("admins change viewers and editors, invite by email and see the link once", async () => {
  const sent: Sent[] = [];
  stubApi("admin", sent);
  renderMembers();
  const adaRole = await screen.findByRole("combobox", { name: "Role of Ada" });
  // An admin cannot change another admin, nor hand out admin.
  expect(screen.queryByRole("combobox", { name: "Role of Katherine" })).toBeNull();
  expect(screen.queryByRole("option", { name: "admin" })).toBeNull();
  fireEvent.change(adaRole, { target: { value: "editor" } });
  await waitFor(() => expect(sent[0]).toMatchObject({ method: "PATCH", url: "/api/v1/workspaces/w1/members/u2", body: { role: "editor" } }));

  fireEvent.change(screen.getByRole("textbox", { name: "Email" }), { target: { value: "alan@demo.test" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Role" }), { target: { value: "editor" } });
  fireEvent.click(screen.getByRole("button", { name: "Create link" }));
  await waitFor(() => expect(sent[1]).toMatchObject({ method: "POST", body: { email: "alan@demo.test", role: "editor" } }));
  expect(await screen.findByRole("textbox", { name: "Invitation link" })).toHaveValue("http://x/invitations/accept#token=abc");
});

test("owners may change admins and make owners", async () => {
  stubApi("owner", []);
  renderMembers();
  await screen.findByRole("combobox", { name: "Role of Katherine" });
  expect(screen.getAllByRole("option", { name: "owner" }).length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "Leave" })).toBeInTheDocument();
});

test("a signed-out invitee signs in first and the invitation is accepted on return", async () => {
  let signedIn = false;
  const accepted: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (!signedIn) return new Response(JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "Sign in.", request_id: null, retryable: false, details: {} } }), { status: 401 });
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      accepted.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify({ workspace_id: "w1" }));
    }),
  );
  const app = () => (
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/invitations/accept"]}>
        <Routes>
          <Route path="/invitations/accept" element={<AcceptInvitationPage />} />
          <Route path="/login" element={<p>Sign in page</p>} />
          <Route path="/w/:wid" element={<p>Workspace page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  window.history.replaceState(null, "", "/invitations/accept#token=secret-token-123456");
  const first = render(app());
  expect(await screen.findByText("Sign in page")).toBeInTheDocument();
  expect(sessionStorage.getItem("codehover.invitation")).toBe("secret-token-123456");
  expect(window.location.hash).toBe(""); // the token leaves the address bar
  first.unmount();

  signedIn = true;
  render(app());
  expect(await screen.findByText("Workspace page")).toBeInTheDocument();
  expect(accepted).toEqual([{ token: "secret-token-123456" }]);
  expect(sessionStorage.getItem("codehover.invitation")).toBeNull();
});
