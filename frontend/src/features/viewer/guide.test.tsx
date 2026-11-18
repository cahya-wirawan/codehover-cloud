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

test("guide Markdown renders as text and citations as source labels", () => {
  const page: GuidePage = {
    slug: "flow",
    title: "Flow",
    ordinal: 1,
    purpose: "",
    status: "written",
    check_status: "supported",
    provenance: {},
    markdown: `# Flow\n\nRuns main [[${FILE}:3-5]]. <img src=x onerror="alert(1)">`,
    citations: [{ file_id: FILE, path: "src/app.py", start_line: 3, end_line: 5 }],
    claims: [],
  };
  const { container } = render(<GuideText page={page} />);
  expect(screen.getByText("[src/app.py:3–5]")).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull(); // model output is never interpreted as HTML
  expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
});
