import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { routes } from "./routes";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

function mockFetch(status: number, body: unknown, headers: Record<string, string> = {}) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status, headers }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

test("home page shows API version from the live endpoint", async () => {
  const fetchMock = mockFetch(200, { status: "ok", version: "0.1.0" });
  renderAt("/");
  expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  expect(await screen.findByText(/API online/)).toHaveTextContent("version 0.1.0");
  expect(fetchMock).toHaveBeenCalledWith("/api/v1/health/live", expect.objectContaining({ credentials: "same-origin" }));
});

test("API error shows an alert with the request reference", async () => {
  mockFetch(503, {
    error: { code: "NOT_READY", message: "Service is not ready.", request_id: "req-1234abcd", retryable: true, details: {} },
  });
  renderAt("/");
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("API unavailable");
  expect(alert).toHaveTextContent("req-1234abcd");
});

test("unknown routes render the not-found page inside the shell", () => {
  mockFetch(200, { status: "ok", version: "0.1.0" });
  renderAt("/does/not/exist");
  expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "codehover-cloud" })).toBeInTheDocument();
});
