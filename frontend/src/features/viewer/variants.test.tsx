import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { routes } from "../../app/routes";

const BUILD = {
  id: "b1", project_id: "p1", snapshot_id: "s1", estimate_id: "e1", retry_of_id: null, cancel_requested_at: null,
  status: "ready", commit_sha: "c0ffee0000000000", snapshot_status: "scanned", rejection_code: null,
  engine_version: "1", prompt_version: "0.4.0", created_at: "2026-11-19T00:00:00Z", completed_at: null,
  language: "en", level: "standard", variant_of: null,
  stages: [], counts: { files: 1, symbols: 1 }, usage: { cap_micro_usd: 0, reserved_micro_usd: 0, settled_micro_usd: 0 },
};

afterEach(() => vi.unstubAllGlobals());

test("readers switch language or level and stay on the same tab and query", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      const replies: Record<string, unknown> = {
        "/api/v1/me": { id: "u1", display_name: "Ada" },
        "/api/v1/builds/b1": BUILD,
        "/api/v1/builds/b2": { ...BUILD, id: "b2", language: "de", level: "beginner", variant_of: "b1" },
        "/api/v1/builds/b1/variants": { items: [{ id: "b1", language: "en", level: "standard", base: true }, { id: "b2", language: "de", level: "beginner", base: false }], next_cursor: null },
        "/api/v1/builds/b2/variants": { items: [{ id: "b1", language: "en", level: "standard", base: true }, { id: "b2", language: "de", level: "beginner", base: false }], next_cursor: null },
        "/api/v1/workspaces": { items: [{ id: "w1", name: "Team", slug: "team", role: "editor" }], next_cursor: null },
        "/api/v1/projects/p1": { id: "p1", workspace_id: "w1", name: "API", slug: "api", tracked_ref: "main", owner: "o", repository: "r" },
      };
      const body = replies[url];
      return new Response(JSON.stringify(body ?? { items: [], next_cursor: null }), { status: body === undefined && url.includes("/guide/") ? 404 : 200 });
    }),
  );
  const router = createMemoryRouter(routes, { initialEntries: ["/p/p1/builds/b1/search?q=main"] });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  const select = await screen.findByRole("combobox", { name: "Read in" });
  expect(screen.getByRole("link", { name: "Make a variant" })).toHaveAttribute("href", "/p/p1?variant_of=b1&snapshot=s1");
  fireEvent.change(select, { target: { value: "b2" } });
  await waitFor(() => expect(router.state.location.pathname).toBe("/p/p1/builds/b2/search"));
  expect(router.state.location.search).toBe("?q=main");
  expect(await screen.findByText("Deutsch, beginner")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Make a variant" })).toBeNull(); // no variants of variants
});
