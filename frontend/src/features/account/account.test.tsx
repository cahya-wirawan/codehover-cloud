import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { AccountPage } from "./AccountPage";

const ME = { id: "u1", display_name: "Ada", primary_email: "ada@example.test" };
const SOLE = {
  error: {
    code: "OWNERSHIP_TRANSFER_REQUIRED",
    message: "Give these workspaces another owner, or delete them with your account.",
    request_id: null,
    retryable: false,
    details: { workspaces: [{ id: "w1", slug: "team", name: "Team" }] },
  },
};

afterEach(() => vi.unstubAllGlobals());

test("account deletion asks for the phrase, then for each workspace owned alone", async () => {
  const bodies: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/me") return new Response(JSON.stringify(ME));
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (url === "/api/v1/me/deletion") {
        const body = JSON.parse(String(init?.body)) as { delete_workspaces: string[] };
        bodies.push(body);
        return body.delete_workspaces.length
          ? new Response(JSON.stringify({ account: {}, workspaces: [{}] }), { status: 202 })
          : new Response(JSON.stringify(SOLE), { status: 409 });
      }
      return new Response("{}", { status: 404 });
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <AccountPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText(/Signed in as Ada/)).toBeInTheDocument();
  const button = screen.getByRole("button", { name: "Delete my account" });
  const phrase = screen.getByRole("textbox", { name: /Type the phrase/ });
  fireEvent.change(phrase, { target: { value: "delete my acount" } });
  expect(button).toBeDisabled();
  fireEvent.change(phrase, { target: { value: "delete my account" } });
  expect(button).toBeEnabled();
  fireEvent.click(button);

  const choice = await screen.findByRole("checkbox", { name: /Delete Team/ });
  expect(button).toBeDisabled(); // every workspace owned alone needs a decision first
  fireEvent.click(choice);
  expect(button).toBeEnabled();
  fireEvent.click(button);
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1]).toEqual({ confirmation: "delete my account", delete_workspaces: ["w1"] });
});
