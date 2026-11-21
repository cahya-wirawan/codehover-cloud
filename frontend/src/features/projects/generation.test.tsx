import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { GenerationPanel } from "./GenerationPanel";

const SNAPSHOT = { id: "s1", status: "scanned", commit_sha: "c".repeat(40), observed_ref: "main", rejection_code: null, scan_summary: { selected: 3 } };

function serve(sent: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (url.startsWith("/api/v1/catalog/presets"))
        return new Response(JSON.stringify({ price_version: "c", items: [{ name: "anthropic-v2", description: "", writer_model: "w", checker_model: "c", providers: ["anthropic"], data_regions: [] }] }));
      if (url === "/api/v1/workspaces/w1/usage") return new Response(JSON.stringify({ available_micro_usd: 10_000_000 }));
      if (url === "/api/v1/projects/p1/snapshots/s1") return new Response(JSON.stringify(SNAPSHOT));
      if (url === "/api/v1/projects/p1/estimates" && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { config: { languages: string[] } };
        sent.push(body);
        return new Response(
          JSON.stringify({
            id: "e1", snapshot_id: "s1", commit_sha: SNAPSHOT.commit_sha, price_version: "c", selected_files: 3, excluded_files: 0,
            estimated_tokens: { low: { input: 1, output: 1 }, high: { input: 2, output: 2 } },
            cost_micro_usd: { low: 200_000, high: 600_000 }, per_language_micro_usd: { low: 100_000, high: 300_000 },
            config: { languages: body.config.languages, levels: ["standard"] },
            expires_at: "2026-11-21T12:00:00Z", expired: false, disclaimer: "An estimate.",
          }),
          { status: 201 },
        );
      }
      return new Response("{}", { status: 404 });
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

test("several languages go into one estimate, which shows the cost of each", async () => {
  const sent: { config: { languages: string[] } }[] = [];
  serve(sent);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/?snapshot=s1"]}>
        <GenerationPanel projectId="p1" workspaceId="w1" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const also = await screen.findByRole("group", { name: "Also write in" });
  expect(also).not.toHaveTextContent("English"); // the base language is not offered twice
  fireEvent.click(screen.getByRole("checkbox", { name: "Bahasa Indonesia" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Deutsch" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Estimate" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Estimate" }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]?.config.languages).toEqual(["en", "id", "de"]);
  expect(await screen.findByText(/English, Bahasa Indonesia, Deutsch: \$0\.10 – \$0\.30 each/)).toBeInTheDocument();
  expect(screen.getByText("$0.20 – $0.60")).toBeInTheDocument(); // every language together
});
