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
        const body = JSON.parse(String(init.body)) as { config: { languages: string[]; translate?: boolean } };
        sent.push(body);
        return new Response(
          JSON.stringify({
            id: "e1", snapshot_id: "s1", commit_sha: SNAPSHOT.commit_sha, price_version: "c", selected_files: 3, excluded_files: 0,
            estimated_tokens: { low: { input: 1, output: 1 }, high: { input: 2, output: 2 } },
            cost_micro_usd: { low: 200_000, high: 600_000 }, per_language_micro_usd: { low: 100_000, high: 300_000 },
            config: { languages: body.config.languages, levels: ["standard"] },
            per_translation_micro_usd: body.config.translate ? { low: 40_000, high: 90_000 } : null,
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

function panel() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/?snapshot=s1"]}>
        <GenerationPanel projectId="p1" workspaceId="w1" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("English comes first; further languages come from the list of 30, translated by default", async () => {
  const sent: { config: { languages: string[]; translate?: boolean } }[] = [];
  serve(sent);
  panel();
  const add = await screen.findByRole("combobox", { name: "Add a language" });
  expect(screen.getByRole("group", { name: "Languages" })).toHaveTextContent("English first.");
  const options = Array.from((add as HTMLSelectElement).options).map((o) => o.value).filter(Boolean);
  expect(options).toHaveLength(29); // the 30 most spoken languages, English already first
  expect(options).not.toContain("en");
  fireEvent.change(add, { target: { value: "id" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Add a language" }), { target: { value: "de" } });
  expect(screen.queryByRole("combobox", { name: "Add a language" })).toBeNull(); // English and two more
  expect(screen.getByRole("checkbox", { name: /Translate the other languages from the first/ })).toBeChecked();
  await waitFor(() => expect(screen.getByRole("button", { name: "Estimate" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Estimate" }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]?.config).toMatchObject({ languages: ["en", "id", "de"], translate: true });
  expect(await screen.findByText(/Bahasa Indonesia, Deutsch, translated: \$0\.04 – \$0\.09 each/)).toBeInTheDocument();
  expect(screen.getByText(/in proportion to these estimates/)).toBeInTheDocument();
});

test("unticking translation writes every language from the source, at one build's cost each", async () => {
  const sent: { config: { languages: string[]; translate?: boolean } }[] = [];
  serve(sent);
  panel();
  fireEvent.change(await screen.findByRole("combobox", { name: "Add a language" }), { target: { value: "ar" } });
  fireEvent.click(screen.getByRole("button", { name: "Remove العربية" }));
  expect(screen.getByRole("group", { name: "Languages" })).toHaveTextContent("English first.");
  fireEvent.change(screen.getByRole("combobox", { name: "Add a language" }), { target: { value: "ja" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /Translate the other languages from the first/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Estimate" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Estimate" }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]?.config.languages).toEqual(["en", "ja"]);
  expect(sent[0]?.config.translate).toBeUndefined();
  expect(await screen.findByText(/English, 日本語: \$0\.10 – \$0\.30 each/)).toBeInTheDocument();
  expect(screen.getByText("$0.20 – $0.60")).toBeInTheDocument(); // every language together
});
