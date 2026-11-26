import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { MemberLimitsPanel } from "./MemberLimitsPanel";

const MEMBERS = {
  period_start: "2026-11-01T00:00:00Z",
  period_end: "2026-12-01T00:00:00Z",
  items: [
    { user_id: "u-owner", display_name: "Olive Owner", handle: "olive", role: "owner", builds: 2, settled_micro_usd: 1_500_000, reserved_micro_usd: 0, limit_micro_usd: null },
    { user_id: "u-editor", display_name: "Eddie Editor", handle: "eddie", role: "editor", builds: 1, settled_micro_usd: 250_000, reserved_micro_usd: 100_000, limit_micro_usd: 1_000_000 },
  ],
  next_cursor: null,
};

function serve(sent: { url: string; body: unknown }[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method === "PUT") {
        sent.push({ url, body: JSON.parse(String(init.body)) });
        return new Response(null, { status: 204 });
      }
      if (url === "/api/v1/workspaces/w1/usage/members") return new Response(JSON.stringify(MEMBERS));
      return new Response("{}", { status: 404 });
    }),
  );
}

function wrap(role: "owner" | "admin") {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <MemberLimitsPanel wid="w1" role={role} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

test("admins see each member's spending and limit editors and viewers only", async () => {
  serve([]);
  wrap("admin");
  const editor = (await screen.findByRole("rowheader", { name: /Eddie Editor/ })).closest("tr") as HTMLElement;
  expect(within(editor).getByText("$1.00 a month")).toBeInTheDocument();
  expect(within(editor).getByText("$0.10 held")).toBeInTheDocument();
  expect(within(editor).getByRole("button", { name: "Change the spending limit of eddie" })).toBeInTheDocument();
  const owner = screen.getByRole("rowheader", { name: /Olive Owner/ }).closest("tr") as HTMLElement;
  expect(within(owner).getByText("No limit")).toBeInTheDocument();
  expect(within(owner).queryByRole("button")).toBeNull(); // admins do not limit owners
});

test("owners set a limit in dollars and remove it", async () => {
  const sent: { url: string; body: unknown }[] = [];
  serve(sent);
  wrap("owner");
  fireEvent.click(await screen.findByRole("button", { name: "Change the spending limit of olive" }));
  const amount = screen.getByRole("textbox", { name: "Monthly limit of olive in US dollars" });
  fireEvent.change(amount, { target: { value: "2.5x" } });
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  fireEvent.change(amount, { target: { value: "2.50" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(sent).toEqual([{ url: "/api/v1/workspaces/w1/members/u-owner/spending-limit", body: { monthly_limit_micro_usd: 2_500_000 } }]),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Change the spending limit of eddie" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove limit" }));
  await waitFor(() => expect(sent[1]).toEqual({ url: "/api/v1/workspaces/w1/members/u-editor/spending-limit", body: { monthly_limit_micro_usd: null } }));
});
