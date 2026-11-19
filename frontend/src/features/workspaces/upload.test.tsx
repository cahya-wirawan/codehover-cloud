import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import { WorkspacePage } from "./WorkspacePage";

afterEach(() => vi.unstubAllGlobals());

type Call = { url: string; type: string | null; body: unknown };

function stub(calls: Call[], created: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method === "POST") {
        calls.push({ url, type: new Headers(init.headers).get("Content-Type"), body: init.body });
        if (url.endsWith("/projects")) return new Response(JSON.stringify(created), { status: 201 });
        return new Response(JSON.stringify({ id: "s1", commit_sha: "a".repeat(64), status: "pending" }), { status: 202 });
      }
      if (url === "/api/v1/workspaces") return new Response(JSON.stringify({ items: [{ id: "w1", name: "Team", slug: "team", role: "owner" }], next_cursor: null }));
      if (url.endsWith("/connections") || url.endsWith("/projects")) return new Response(JSON.stringify({ items: [], next_cursor: null }));
      return new Response(JSON.stringify({ items: [], next_cursor: null }));
    }),
  );
}

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/w/w1"]}>
        <Routes>
          <Route path="/w/:wid" element={<WorkspacePage />} />
          <Route path="/p/:pid" element={<p>Project page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("without a GitHub connection a project can come from an uploaded ZIP", async () => {
  const calls: Call[] = [];
  stub(calls, { id: "p1", workspace_id: "w1", name: "Demo", slug: "demo", tracked_ref: "upload", owner: "upload", repository: "demo", source: "upload" });
  renderPage();
  // Without a connection the form starts on a public repository; choose the archive instead.
  expect(await screen.findByRole("radio", { name: "A public GitHub repository" })).toBeChecked();
  fireEvent.click(screen.getByRole("radio", { name: "An uploaded ZIP archive" }));
  expect(screen.getByRole("button", { name: "Create project" })).toBeDisabled(); // no archive chosen yet
  const file = new File([new Uint8Array([0x50, 0x4b, 0x03, 0x04])], "demo.zip", { type: "application/zip" });
  fireEvent.change(screen.getByLabelText("Archive (ZIP)"), { target: { files: [file] } });
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Demo" } });
  fireEvent.change(screen.getByLabelText("Address"), { target: { value: "demo" } });
  expect(screen.getByRole("button", { name: "Create project" })).not.toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Create project" }));
  expect(await screen.findByText("Project page")).toBeInTheDocument();
  await waitFor(() => expect(calls.map((c) => c.url)).toEqual(["/api/v1/workspaces/w1/projects", "/api/v1/projects/p1/uploads"]));
  expect(JSON.parse(String(calls[0]?.body))).toEqual({ source: "upload", name: "Demo", slug: "demo" });
  expect(calls[1]?.type).toBe("application/zip");
});

test("a public GitHub repository needs no connection", async () => {
  const calls: Call[] = [];
  stub(calls, { id: "p2", workspace_id: "w1", name: "Hello", slug: "hello", tracked_ref: "master", owner: "octocat", repository: "Hello-World", source: "public" });
  renderPage();
  expect(await screen.findByRole("radio", { name: "A public GitHub repository" })).toBeChecked();
  expect(screen.getByRole("button", { name: "Create project" })).toBeDisabled(); // no repository yet
  fireEvent.change(screen.getByLabelText("Public repository"), { target: { value: " https://github.com/octocat/Hello-World " } });
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Hello" } });
  fireEvent.change(screen.getByLabelText("Address"), { target: { value: "hello" } });
  fireEvent.click(screen.getByRole("button", { name: "Create project" }));
  expect(await screen.findByText("Project page")).toBeInTheDocument();
  expect(JSON.parse(String(calls[0]?.body))).toEqual({
    source: "public",
    public_repository: "https://github.com/octocat/Hello-World",
    tracked_ref: "",
    name: "Hello",
    slug: "hello",
  });
});
