import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ProviderKeyPanel } from "./ProviderKeyPanel";

afterEach(() => vi.unstubAllGlobals());

function serve(saved: { url?: string; body: unknown }[], existing: boolean) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method === "PUT") {
        saved.push({ url, body: JSON.parse(String(init.body)) });
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
  const saved: { url?: string; body: unknown }[] = [];
  serve(saved, false);
  panel(true);
  const input = await screen.findByLabelText("Anthropic API key");
  expect(input).toHaveAttribute("type", "password");
  const button = screen.getByRole("button", { name: "Check and save the Anthropic key" });
  expect(button).toBeDisabled();
  fireEvent.change(input, { target: { value: "sk-ant-test-" + "x".repeat(30) } });
  fireEvent.click(button);
  await waitFor(() =>
    expect(saved).toEqual([
      { url: "/api/v1/workspaces/w1/provider-keys/anthropic", body: { api_key: "sk-ant-test-" + "x".repeat(30) } },
    ]),
  );
  await waitFor(() => expect(input).toHaveValue(""));
});

test("admins see the hint but cannot change the key", async () => {
  serve([], true);
  panel(false);
  expect(await screen.findByText("sk-ant-…abcd")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Remove key/ })).toBeNull();
  expect(screen.queryByLabelText(/API key/)).toBeNull();
});

test("each provider has its own key, saved to its own path", async () => {
  const saved: { url?: string; body: unknown }[] = [];
  serve(saved, true);
  panel(true);
  expect(await screen.findByText("sk-ant-…abcd")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Remove key for Anthropic" })).toBeInTheDocument();
  for (const name of ["OpenAI", "Gemini", "OpenRouter"]) {
    expect(screen.getByLabelText(`${name} API key`)).toHaveAttribute("type", "password");
    expect(screen.queryByRole("button", { name: `Remove key for ${name}` })).toBeNull();
  }
  fireEvent.change(screen.getByLabelText("OpenRouter API key"), { target: { value: "sk-or-v1-" + "y".repeat(30) } });
  fireEvent.click(screen.getByRole("button", { name: "Check and save the OpenRouter key" }));
  await waitFor(() =>
    expect(saved).toEqual([
      { url: "/api/v1/workspaces/w1/provider-keys/openrouter", body: { api_key: "sk-or-v1-" + "y".repeat(30) } },
    ]),
  );
});
