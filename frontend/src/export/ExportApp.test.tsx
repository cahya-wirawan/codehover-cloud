import { fireEvent, render, screen } from "@testing-library/react";

import { ExportApp } from "./ExportApp";
import type { Kb } from "./staticSource";

const FILE = "00000000-0000-4000-8000-00000000000a";

function kb(language: string, title: string, lines: string[]): Kb {
  return {
    project: { name: "demo", repository: "o/demo" },
    build: { commit_sha: "c".repeat(40), created_at: "2026-11-21T00:00:00Z", variant: `${language}-standard` },
    overview: null,
    files: [{ id: FILE, path: "src/app.py", language: "python", sha256: "0".repeat(64), lines, explanation: null, references: [] }],
    symbols: [],
    guide: [{ slug: "flow", title, section: "", purpose: "", markdown: `# ${title}\n\nText [[${FILE}:1-1]].`, check_status: "supported", claims: [] }],
  };
}

test("the offline copy switches between the languages in the archive and keeps the code", async () => {
  window.location.hash = "#/guide/flow";
  render(<ExportApp kb={kb("en", "How it runs", ["def main(): pass"])} variants={[kb("de", "Wie es läuft", [])]} />);
  expect(await screen.findByRole("heading", { name: "How it runs" })).toBeInTheDocument();
  fireEvent.change(screen.getByRole("combobox", { name: "Language" }), { target: { value: "de" } });
  expect(await screen.findByRole("heading", { name: "Wie es läuft" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "Code" }));
  fireEvent.click(await screen.findByRole("button", { name: "src" }));
  fireEvent.click(await screen.findByRole("button", { name: "app.py" }));
  expect(await screen.findByText("main")).toBeInTheDocument(); // the variant reads the main build's lines
});
