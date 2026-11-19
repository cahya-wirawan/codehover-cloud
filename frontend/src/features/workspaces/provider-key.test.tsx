import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ProviderKeyPanel } from "./ProviderKeyPanel";

afterEach(() => vi.unstubAllGlobals());

function serve(saved: { body: unknown }[], existing: boolean) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method === "PUT") {
        saved.push({ body: JSON.parse(String(init.body)) });
        return new Response(JSON.stringify({ provider: "anthropic", hint: "sk-ant-…cdef", created_at: "2026-11-19T00:00:00Z", created_by_name: "Ada", verified_at: "2026-11-19T00:00:00Z" }));
      }
      const items = existing ? [{ provider: "anthropic", hint: "sk-ant-…abcd", created_at: "2026-11-19T00:00:00Z", created_by_name: "Ada", verified_at: null }] : [];
      return new Response(JSON.stringify({ items, next_cursor: null }));
    }),
  );
}

function panel(owner: boolean) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ProviderKeyPanel workspaceId="w1" owner={owner} />
    </QueryClientProvider>,
  );
}

test("owners save a key as a password field and only ever see its hint", async () => {
  const saved: { body: unknown }[] = [];
  serve(saved, false);
  panel(true);
  const input = await screen.findByLabelText("Anthropic API key");
  expect(input).toHaveAttribute("type", "password");
  const button = screen.getByRole("button", { name: "Check and save" });
  expect(button).toBeDisabled();
  fireEvent.change(input, { target: { value: "sk-ant-test-" + "x".repeat(30) } });
  fireEvent.click(button);
  await waitFor(() => expect(saved).toEqual([{ body: { api_key: "sk-ant-test-" + "x".repeat(30) } }]));
  await waitFor(() => expect(input).toHaveValue(""));
});

test("admins see the hint but cannot change the key", async () => {
  serve([], true);
  panel(false);
  expect(await screen.findByText("sk-ant-…abcd")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Remove key" })).toBeNull();
  expect(screen.queryByLabelText(/API key/)).toBeNull();
});
