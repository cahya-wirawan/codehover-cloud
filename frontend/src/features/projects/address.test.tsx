import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AddressPanel } from "./AddressPanel";

afterEach(() => vi.unstubAllGlobals());

const PROJECT = {
  id: "p1", workspace_id: "w1", name: "micrograd", slug: "micrograd-2", tracked_ref: "main", owner: "o",
  repository: "micrograd", address: "cahya/micrograd-2",
};

test("an admin changes the project's address and sees where the site will live", async () => {
  const sent: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      sent.push({ url, body: JSON.parse(String(init?.body)) });
      return new Response(JSON.stringify({ ...PROJECT, slug: "micrograd", address: "cahya/micrograd" }));
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AddressPanel project={PROJECT} />
    </QueryClientProvider>,
  );
  const field = screen.getByLabelText("Project address");
  expect(screen.getByText("The site is /sites/cahya/micrograd-2.")).toBeInTheDocument();
  const button = screen.getByRole("button", { name: "Change address" });
  expect(button).toBeDisabled();
  fireEvent.change(field, { target: { value: "MicroGrad" } });
  expect(screen.getByText("The site is /sites/cahya/micrograd.")).toBeInTheDocument();
  fireEvent.click(button);
  await waitFor(() => expect(sent).toEqual([{ url: "/api/v1/projects/p1/address", body: { slug: "micrograd" } }]));
});
