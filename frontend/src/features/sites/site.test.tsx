import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { routes } from "../../app/routes";

const SITE = { address: "o/demo", project: "demo", repository: "o/demo", commit_sha: "c".repeat(40), visibility: "public", generated: true, published_at: "2026-11-21T00:00:00Z", license: null };
const index = (title: string) => ({
  overview: { overview: `${title} overview`, status: "generated", key_concepts: [], reading_order: [] },
  pages: [{ slug: "flow", title, section: "", ordinal: 1, purpose: "", status: "written", check_status: "supported", provenance: {} }],
});

test("a published site switches between the languages its build was written in", async () => {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      asked.push(url);
      const replies: Record<string, unknown> = {
        "/api/v1/me": undefined,
        "/api/v1/published/o/demo": SITE,
        "/api/v1/published/o/demo/variants": { items: [{ language: "en", published: true }, { language: "de", published: false }] },
        "/api/v1/published/o/demo/guide": index("How it works"),
        "/api/v1/published/o/demo/guide?language=de": index("Wie es funktioniert"),
      };
      const body = replies[url];
      return body === undefined
        ? new Response(JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "x", request_id: null, retryable: false, details: {} } }), { status: 401 })
        : new Response(JSON.stringify(body));
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/sites/o/demo"] })} />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("button", { name: /How it works/ })).toBeInTheDocument();
  const select = await screen.findByRole("combobox", { name: "Language" });
  fireEvent.change(select, { target: { value: "de" } });
  expect(await screen.findByRole("button", { name: /Wie es funktioniert/ })).toBeInTheDocument();
  await waitFor(() => expect(asked).toContain("/api/v1/published/o/demo/guide?language=de"));
  expect(select).toHaveValue("de");
});

afterEach(() => vi.unstubAllGlobals());
