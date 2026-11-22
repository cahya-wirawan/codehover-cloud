import { useEffect, useId, useState } from "react";

import { drawable } from "./mermaidPolicy";

/**
 * A guide page's Mermaid block, drawn as a picture (19 P04). The backend already refuses click, style,
 * init and HTML directives; here Mermaid runs with `securityLevel: "strict"` and SVG text labels, and its
 * SVG is shown through an <img> data URL. A browser renders an SVG image without scripts, event
 * handlers or external loads, so nothing from model output becomes part of this document. Until the
 * diagram is drawn, or if it cannot be, the source is shown as code.
 */
type Mermaid = (typeof import("mermaid"))["default"];

let loading: Promise<Mermaid> | null = null;
let configuredDark: boolean | null = null;

function dark(): boolean {
  const theme = document.documentElement.dataset.theme;
  if (theme === "dark") return true;
  if (theme === "light") return false;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

async function mermaid(): Promise<Mermaid> {
  loading ??= import("mermaid").then((m) => m.default);
  const instance = await loading;
  const isDark = dark();
  if (configuredDark !== isDark) {
    instance.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: isDark ? "dark" : "neutral",
      // System fonts: an SVG image cannot load the page's web fonts, so text is measured and drawn alike.
      fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
      htmlLabels: false,
      flowchart: { htmlLabels: false },
    });
    configuredDark = isDark;
  }
  return instance;
}

let queue: Promise<unknown> = Promise.resolve(); // Mermaid renders one diagram at a time

export function MermaidDiagram({ code }: { code: string }) {
  const id = `mmd-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const [src, setSrc] = useState<string | null>(null);
  const [width, setWidth] = useState<number | null>(null);
  const allowed = drawable(code);
  const [failed, setFailed] = useState(!allowed);
  useEffect(() => {
    if (!allowed) return;
    let live = true;
    const job = queue.then(async () => {
      try {
        const { svg } = await (await mermaid()).render(id, code);
        // An SVG image whose width is "100%" has no size of its own; its viewBox gives the drawn width.
        const box = /viewBox="[-\d.]+ [-\d.]+ ([\d.]+) [\d.]+"/.exec(svg);
        if (live) {
          setWidth(box ? Math.ceil(Number(box[1])) : null);
          setSrc(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`);
        }
      } catch {
        if (live) setFailed(true);
      } finally {
        document.getElementById(id)?.remove(); // Mermaid's measuring element, if a failure left it behind
      }
    });
    queue = job;
    return () => {
      live = false;
    };
  }, [allowed, code, id]);
  return (
    <figure className="diagram" data-state={src ? "drawn" : failed ? "failed" : "pending"}>
      {src ? (
        <img src={src} alt="Diagram; its Mermaid source follows." className="diagram-image" {...(width ? { width } : {})} />
      ) : null}
      <details className="diagram-source" open={failed || undefined}>
        <summary>{failed ? "This diagram could not be drawn; its source:" : "Diagram source"}</summary>
        <pre>
          <code>{code}</code>
        </pre>
      </details>
    </figure>
  );
}
