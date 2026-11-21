import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import { formatUsd, type GuideIndex, type GuidePage, type ViewerSource } from "../../lib/api/resources";
import { GuidePanel, GuideText, OverviewText } from "./GuidePanel";

// Mermaid needs a real layout engine; this stand-in returns SVG carrying a script, which must never run or
// become part of the document.
vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (_id: string, code: string) => {
      if (code.includes("broken")) throw new Error("parse");
      return { svg: `<svg><script>window.__pwned=1</script><text>${code}</text></svg>` };
    }),
  },
}));

const FILE = "019a90a0-0000-7000-8000-000000000001";

test("micro-USD amounts are shown rounded up to the cent", () => {
  expect(formatUsd(0)).toBe("$0.00");
  expect(formatUsd(1)).toBe("$0.01");
  expect(formatUsd(1_230_000)).toBe("$1.23");
  expect(formatUsd(50_000_000)).toBe("$50.00");
  expect(formatUsd(1_234_567_890_000)).toBe("$1,234,567.89");
});

function guidePage(markdown: string): GuidePage {
  return {
    slug: "flow",
    title: "Flow",
    section: "Basics",
    ordinal: 1,
    purpose: "",
    status: "written",
    check_status: "supported",
    provenance: {},
    markdown,
    citations: [{ file_id: FILE, path: "src/app.py", start_line: 3, end_line: 5 }],
    claims: [],
    symbols: [],
  };
}

test("guide Markdown renders as elements, with raw HTML as text and citations as source labels", () => {
  const { container } = render(
    <GuideText page={guidePage(`# Flow\n\nRuns main [[${FILE}:3-5]]. <img src=x onerror="alert(1)">`)} />,
  );
  expect(screen.getByRole("heading", { level: 3, name: "Flow" })).toBeInTheDocument();
  expect(screen.getByText("[src/app.py:3–5]")).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull(); // model output is never interpreted as HTML
  expect(container.querySelector("p")?.textContent).toContain('<img src=x onerror="alert(1)">');
});

test("headings, lists, emphasis and code render; citations work inside them", () => {
  const markdown = [
    "# Flow",
    "",
    "## How a request is handled",
    "",
    `The **router** calls \`handle()\` [[${FILE}:3-5]] and then:`,
    "",
    `1. parses the body [[${FILE}:3-5]]`,
    "2. *validates* it",
    "",
    "- one",
    "- two",
    "",
    "> Unknown: retries are not shown.",
    "",
    "```python",
    "def handle(): <b>not html</b>",
    "```",
  ].join("\n");
  const { container } = render(<GuideText page={guidePage(markdown)} />);
  expect(screen.getByRole("heading", { level: 4, name: "How a request is handled" })).toBeInTheDocument();
  expect(container.querySelector("strong")?.textContent).toBe("router");
  expect(container.querySelector("p code")?.textContent).toBe("handle()");
  const ordered = container.querySelectorAll("ol > li");
  expect(ordered).toHaveLength(2);
  expect(ordered[0]?.textContent).toContain("[src/app.py:3–5]");
  expect(ordered[1]?.querySelector("em")?.textContent).toBe("validates");
  expect(container.querySelectorAll("ul > li")).toHaveLength(2);
  expect(container.querySelector("blockquote")?.textContent).toContain("Unknown: retries are not shown.");
  expect(container.querySelector("pre code")?.textContent).toBe("def handle(): <b>not html</b>\n");
  expect(container.querySelector("b")).toBeNull();
  expect(screen.getAllByText("[src/app.py:3–5]")).toHaveLength(2);
});

test("only https and in-page links become links; images show their alt text", () => {
  const markdown = [
    "# Links",
    "",
    "[docs](https://example.com/docs) [top](#links) [bad](javascript:alert(1)) [data](data:text/html,x)",
    "[plain](http://example.com) ![diagram of the flow](https://example.com/x.png)",
  ].join("\n");
  const { container } = render(<GuideText page={guidePage(markdown)} />);
  const links = Array.from(container.querySelectorAll("a"));
  expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://example.com/docs", "#links"]);
  expect(links[0]?.getAttribute("rel")).toBe("noopener noreferrer nofollow");
  expect(container.textContent).toContain("bad");
  expect(container.textContent).toContain("plain");
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toContain("diagram of the flow");
});

test("the overview renders its Markdown like a guide page", () => {
  const overview = [
    "A **CLI** that turns `main()` into a site.",
    "",
    "## Parts",
    "",
    "- the parser",
    "- the *renderer*",
    "",
    "Second paragraph <script>x</script> [docs](https://example.com) [bad](javascript:alert(1)).",
  ].join("\n");
  const { container } = render(<OverviewText overview={overview} />);
  expect(container.querySelector("strong")?.textContent).toBe("CLI");
  expect(container.querySelector("code")?.textContent).toBe("main()");
  expect(screen.getByRole("heading", { level: 4, name: "Parts" })).toBeInTheDocument();
  expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["the parser", "the renderer"]);
  expect(container.querySelectorAll("p")).toHaveLength(2);
  expect(container.querySelector("script")).toBeNull();
  expect(container.textContent).toContain("<script>x</script>");
  expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute("href", "https://example.com");
  expect(screen.queryByRole("link", { name: "bad" })).toBeNull();
});

test("tables render as tables, guide links stay inside the guide, and diagrams are pictures", async () => {
  const markdown = [
    "# Options",
    "",
    "| Option | Meaning |",
    "|:--|:-:|",
    `| \`-i\` | input file [[${FILE}:3-5]] |`,
    "",
    "See [the flow](guide:flow) and [missing](guide:Not_A_Slug).",
    "",
    "```mermaid",
    "flowchart TD",
    "  A --> B",
    "```",
  ].join("\n");
  const { container } = render(
    <MemoryRouter>
      <GuideText page={guidePage(markdown)} codeBase="/p/p1/builds/b1" />
    </MemoryRouter>,
  );
  expect(screen.getByRole("columnheader", { name: "Meaning" })).toHaveStyle({ textAlign: "center" });
  expect(screen.getByRole("cell", { name: /input file/ })).toHaveTextContent("[src/app.py:3–5]");
  expect(screen.getByRole("link", { name: "the flow" })).toHaveAttribute("href", "/p/p1/builds/b1/guide/flow");
  expect(screen.queryByRole("link", { name: "missing" })).toBeNull();
  const picture = await screen.findByRole("img", { name: /Diagram/ });
  expect(picture.getAttribute("src")).toMatch(/^data:image\/svg\+xml;charset=utf-8,/);
  expect(container.querySelector("svg:not(.lucide), script")).toBeNull(); // the SVG is an image, not markup
  expect(container.querySelector("figure pre code")?.textContent).toContain("A --> B"); // its source stays readable
  expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
});

test("a diagram that cannot be drawn shows its source", async () => {
  render(<GuideText page={guidePage("# D\n\n```mermaid\nbroken\n```")} />);
  await waitFor(() => expect(screen.getByText(/could not be drawn/)).toBeInTheDocument());
  expect(screen.queryByRole("img")).toBeNull();
});

const INDEX: GuideIndex = {
  overview: {
    overview: "A **converter** from JSON Lines to binidx.",
    status: "generated",
    key_concepts: [{ term: "binidx", explanation: "A token file plus an index." }],
    reading_order: [{ file_id: FILE, reason: "Everything starts here.", path: "src/main.rs" }],
  },
  pages: [
    { slug: "architecture", title: "Architecture", section: "Basics", ordinal: 1, purpose: "The shape of a run.", status: "written", check_status: "supported", provenance: {} },
    { slug: "workers", title: "Workers", section: "Conversion", ordinal: 2, purpose: "How threads split the file.", status: "written", check_status: "partial", provenance: {} },
  ],
};

function fakeSource(): ViewerSource {
  return {
    key: "t",
    tree: vi.fn(),
    file: vi.fn(),
    symbol: vi.fn(),
    guide: async () => INDEX,
    guidePage: async (slug: string) => ({
      ...guidePage(`# ${slug === "workers" ? "Workers" : "Architecture"}\n\nUses \`json2bin()\` and \`nowhere\`.`),
      slug,
      symbols: [{ mention: "json2bin", id: "s9", name: "json2bin", kind: "function", file_id: FILE, start_line: 79 }],
    }),
    search: vi.fn(),
    quality: null,
  } as unknown as ViewerSource;
}

function renderGuide(path: string) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/b/guide" element={<GuidePanel source={fakeSource()} base="/b" />} />
          <Route path="/b/guide/:slug" element={<GuidePanel source={fakeSource()} base="/b" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("the guide's landing view shows the overview, key ideas, where to start and the pages by section", async () => {
  renderGuide("/b/guide");
  expect(await screen.findByText("converter", { selector: "strong" })).toBeInTheDocument();
  expect(screen.getByText("binidx", { selector: "dt" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "src/main.rs" })).toHaveAttribute("href", `/b/code?file=${FILE}`);
  const nav = screen.getByRole("navigation", { name: "Guide pages" });
  expect(nav).toHaveTextContent(/^BasicsArchitectureConversionWorkersPartly supported$/);
  fireEvent.click(screen.getByRole("link", { name: "Workers" }));
  expect(await screen.findByRole("heading", { name: "Workers" })).toBeInTheDocument();
});

test("a page links the definitions it names and offers the previous and next pages", async () => {
  renderGuide("/b/guide/architecture");
  const named = await screen.findByRole("link", { name: "json2bin()" });
  expect(named).toHaveAttribute("href", `/b/code?file=${FILE}&line=79&symbol=s9`);
  expect(screen.queryByRole("link", { name: "nowhere" })).toBeNull(); // an unknown name stays plain code
  const pager = screen.getByRole("navigation", { name: "Next and previous pages" });
  expect(pager).toHaveTextContent("NextWorkers");
  expect(pager).not.toHaveTextContent("Previous");
});
