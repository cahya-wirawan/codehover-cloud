import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AccessPanel } from "./AccessPanel";

afterEach(() => vi.unstubAllGlobals());

test("admins restrict a project and grant members a role on it", async () => {
  const sent: { method: string; url: string; body: unknown; ifMatch: string | null }[] = [];
  let mode = "workspace";
  let members: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method && init.method !== "GET") {
        sent.push({ method: init.method, url, body: init.body ? JSON.parse(String(init.body)) : null, ifMatch: new Headers(init.headers).get("If-Match") });
        if (url.endsWith("/access")) mode = "restricted";
        else members = [{ user_id: "u2", name: "Grace", role: "editor", workspace_role: "viewer", created_at: "2026-11-19T00:00:00Z" }];
        return new Response(JSON.stringify({ mode, version: 4, members }));
      }
      if (url === "/api/v1/projects/p1/access")
        return new Response(JSON.stringify({ mode, version: 3, members }), { headers: { ETag: '"project-access-3"' } });
      if (url.startsWith("/api/v1/workspaces/w1/members"))
        return new Response(JSON.stringify({ items: [
          { user_id: "u1", display_name: "Ada", role: "owner", created_at: "2026-11-19T00:00:00Z" },
          { user_id: "u2", display_name: "Grace", role: "viewer", created_at: "2026-11-19T00:00:00Z" },
        ], next_cursor: null }));
      return new Response("{}", { status: 404 });
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AccessPanel projectId="p1" workspaceId="w1" />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Restrict access" }));
  await waitFor(() => expect(sent[0]).toMatchObject({ method: "PUT", body: { mode: "restricted" }, ifMatch: '"project-access-3"' }));
  const member = await screen.findByRole("combobox", { name: "Member" });
  expect(screen.queryByRole("option", { name: /Ada/ })).toBeNull(); // owners always have access
  fireEvent.change(member, { target: { value: "u2" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Can" }), { target: { value: "editor" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  await waitFor(() => expect(sent[1]).toMatchObject({ method: "PUT", url: "/api/v1/projects/p1/access/members/u2", body: { role: "editor" } }));
  expect(await screen.findByText("Grace", { selector: "span" })).toBeInTheDocument();
  expect(screen.getByText((_, node) => node?.textContent === "Can build · viewer in the workspace")).toBeInTheDocument();
});
