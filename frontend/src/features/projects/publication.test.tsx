import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";

import { routes } from "../../app/routes";
import { PublicationPanel } from "./PublicationPanel";

const PROJECT = { id: "p1", workspace_id: "w1", name: "API", slug: "api", tracked_ref: "main", owner: "octo", repository: "api" };
const QUALITY = {
  claims: 3, counts: { supported: 2, partial: 1, unsupported: 0, unchecked: 0, invalid: 0 }, waived: { unsupported: 0, partial: 0 },
  supported_fraction: 0.67, uncited_sentences: 0, pages: { total: 1, failed: 0 }, publishable: true, blockers: [],
  requires_acknowledgment: true,
};

function fakeApi(replies: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      const body = replies[url];
      return body === undefined
        ? new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "x", request_id: null, retryable: false, details: {} } }), { status: 404 })
        : new Response(JSON.stringify(body), { status: 200 });
    }),
  );
}

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

test("public publishing needs the typed project name and an acknowledgment", async () => {
  fakeApi({
    "/api/v1/projects/p1/builds": { items: [{ id: "b1", status: "ready", commit_sha: "c0ffee000000", created_at: "2026-11-18T00:00:00Z" }], next_cursor: null },
    "/api/v1/builds/b1/quality": QUALITY,
  });
  wrap(<PublicationPanel project={PROJECT} canPublish />);
  expect(await screen.findByText(/Not published/)).toBeInTheDocument();
  const button = await screen.findByRole("button", { name: "Publish" });
  const ack = await screen.findByRole("checkbox", { name: /partly supported/ });
  expect(button).toBeDisabled(); // partly supported claims need the acknowledgment
  fireEvent.click(ack);
  expect(button).toBeEnabled(); // private by default
  fireEvent.click(screen.getByRole("radio", { name: /public/ }));
  expect(button).toBeDisabled();
  expect(screen.queryByRole("textbox", { name: /Site address/ })).toBeNull(); // 0039: owner/project, not chosen
  fireEvent.change(screen.getByRole("textbox", { name: /Type the project name/ }), { target: { value: "api" } });
  expect(button).toBeDisabled(); // the name must match exactly
  fireEvent.change(screen.getByRole("textbox", { name: /Type the project name/ }), { target: { value: "API" } });
  expect(button).toBeEnabled();
});

test("members without the admin role see the state but no controls", async () => {
  fakeApi({ "/api/v1/projects/p1/builds": { items: [], next_cursor: null } });
  wrap(<PublicationPanel project={PROJECT} canPublish={false} />);
  expect(await screen.findByText(/Only workspace admins can publish/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
});

test("a withdrawn or private site is simply not found", async () => {
  fakeApi({});
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/sites/someone/gone-site"] })} />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("heading", { name: "Site not found" })).toBeInTheDocument();
  expect(screen.getByText(/no longer published, or is private/)).toBeInTheDocument();
});

test("a public site of a public repository names its license and links to the license file", async () => {
  fakeApi({
    "/api/v1/published/octo/hello": {
      address: "octo/hello", project: "Hello", repository: "octocat/hello", commit_sha: "feed00000000aaaa", visibility: "public", generated: false,
      published_at: "2026-11-20T00:00:00Z", license: { spdx_id: "MIT", path: "LICENSE", file_id: "f-license" },
    },
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/sites/octo/hello/code"] })} />
    </QueryClientProvider>,
  );
  const link = await screen.findByRole("link", { name: "MIT license" });
  expect(link).toHaveAttribute("href", "/sites/octo/hello/code?file=f-license");
});

test("a license suspension tells the admin to publish again", async () => {
  fakeApi({
    "/api/v1/projects/p1/publication": {
      build_id: "b1", visibility: "public", address: "octo/api", version: 2, acknowledged_partial: false,
      suspended_at: "2026-11-20T00:00:00Z", suspended_reason: "license", updated_at: "2026-11-20T00:00:00Z",
      commit_sha: "c0ffee000000", license_spdx: null,
    },
    "/api/v1/projects/p1/builds": { items: [], next_cursor: null },
  });
  wrap(<PublicationPanel project={PROJECT} canPublish />);
  expect(await screen.findByText(/published before licenses were checked/)).toBeInTheDocument();
});

test("an old one-segment address leads to the site's owner and project", async () => {
  fakeApi({
    "/api/v1/site-aliases/old-site": { owner: "octo", project: "hello" },
    "/api/v1/published/octo/hello": {
      address: "octo/hello", project: "Hello", repository: "octocat/hello", commit_sha: "feed00000000aaaa", visibility: "public",
      generated: false, published_at: "2026-11-20T00:00:00Z", license: null,
    },
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { initialEntries: ["/sites/old-site/code"] });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(router.state.location.pathname).toBe("/sites/octo/hello/code"));
});
