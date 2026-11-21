import { type Kb, staticSource } from "./staticSource";

const A = "00000000-0000-4000-8000-00000000000a";
const B = "00000000-0000-4000-8000-00000000000b";
const KB: Kb = {
  project: { name: "demo", repository: "o/demo" },
  build: { commit_sha: "c".repeat(40), created_at: "2026-11-21T00:00:00Z", variant: "en-standard" },
  overview: { overview: "x", key_concepts: [], reading_order: [{ file_id: A, reason: "start" }] },
  files: [
    {
      id: A, path: "src/app.py", language: "python", sha256: "0".repeat(64),
      lines: ["from util import helper", "def main(argv):", "    return helper(argv)"],
      explanation: { status: "generated", summary: "App.", chunks: [{ start_line: 1, end_line: 3, summary: "", status: "generated" }] },
      references: [
        { line: 3, column: 11, targets: ["h"], resolution: "index", ambiguous: false },
        { line: 3, column: 11, targets: ["gone"], resolution: "name", ambiguous: false },
      ],
    },
    { id: B, path: "src/util.py", language: "python", sha256: "1".repeat(64), lines: ["def helper(args):", "    return args"], explanation: null },
  ],
  symbols: [
    { id: "m", file_id: A, key: "main", name: "main", kind: "function", start_line: 2, end_line: 3, signature: "def main(argv)", card: { summary: "Entry.", params: [{ name: "argv", description: "Words." }] }, uses: ["h"] },
    { id: "h", file_id: B, key: "helper", name: "helper", kind: "function", start_line: 1, end_line: 2, signature: null, card: { summary: "Counts." }, uses: [] },
  ],
  guide: [
    { slug: "flow", title: "Flow", section: "Basics", purpose: "How it runs.", markdown: `# Flow\n\n\`main()\` calls \`helper\` [[${A}:2-3]].`, check_status: "partial", claims: [{ key: "c", text: "t", status: "supported", reason: "", waived: false }] },
  ],
};

test("the export answers the viewer's requests from the archive's data", async () => {
  const source = staticSource(KB);
  expect((await source.tree("")).items.map((e) => e.path)).toEqual(["src"]);
  expect((await source.tree("src")).items.map((e) => e.name)).toEqual(["app.py", "util.py"]);

  const file = await source.file(A, 2);
  expect(file.lines).toEqual(["def main(argv):", "    return helper(argv)"]);
  expect(file.symbols[0]?.params).toEqual(["argv"]);
  expect(file.references).toEqual([
    { line: 3, column: 11, resolution: "index", ambiguous: false, targets: [{ id: "h", name: "helper", kind: "function" }] },
  ]); // a reference to a definition the archive does not hold is dropped

  const helper = await source.symbol("h");
  expect(helper.used_by).toEqual([expect.objectContaining({ id: "m", resolution: "index", file_id: A })]);
  const main = await source.symbol("m");
  expect(main.guides).toEqual([{ slug: "flow", title: "Flow" }]);
  expect(helper.guides).toEqual([]);

  const index = await source.guide();
  expect(index.pages[0]).toMatchObject({ section: "Basics", purpose: "How it runs.", check_status: "partial" });
  expect(index.overview?.reading_order[0]?.path).toBe("src/app.py");
  const page = await source.guidePage("flow");
  expect(page.citations).toEqual([{ file_id: A, path: "src/app.py", start_line: 2, end_line: 3 }]);
  expect(page.symbols.map((s) => s.mention).sort()).toEqual(["helper", "main"]);
  expect(page.claims[0]).toMatchObject({ claim_key: "c", reason: null });

  const hits = (await source.search("help", null)).items;
  expect(hits[0]).toMatchObject({ kind: "symbol", name: "helper", basis: "name_prefix" });
  expect((await source.search("helper", "symbol")).items[0]?.basis).toBe("exact_name");
});
