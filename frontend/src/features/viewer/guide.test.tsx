import { render, screen } from "@testing-library/react";

import { formatUsd, type GuidePage } from "../../lib/api/resources";
import { GuideText } from "./GuidePanel";

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
    ordinal: 1,
    purpose: "",
    status: "written",
    check_status: "supported",
    provenance: {},
    markdown,
    citations: [{ file_id: FILE, path: "src/app.py", start_line: 3, end_line: 5 }],
    claims: [],
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
