import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FolderGit2 } from "lucide-react";
import { useMemo, useState } from "react";
import { HashRouter, NavLink, Route, Routes } from "react-router";

import { LANGUAGES } from "../lib/api/resources";
import { cn } from "../lib/cn";
import { CodeViewer } from "../features/viewer/CodeViewer";
import { GuidePanel } from "../features/viewer/GuidePanel";
import { SearchPanel } from "../features/viewer/SearchPanel";
import { type Kb, staticSource } from "./staticSource";

const languageOf = (kb: Kb) => kb.build.variant.split("-")[0] ?? "en";

/** A variant's knowledge base with the main build's source lines (the same snapshot; the archive keeps
 * them once). */
function withLines(variant: Kb, main: Kb): Kb {
  const lines = new Map(main.files.map((f) => [f.id, f.lines]));
  return { ...variant, files: variant.files.map((f) => ({ ...f, lines: f.lines.length ? f.lines : (lines.get(f.id) ?? []) })) };
}

/** The offline export (10): the product's reading view over the knowledge base in the archive, in each
 * language the build was written in (FR-026). Routes live in the URL fragment, so the copy works from any
 * folder a local web server serves. */
export function ExportApp({ kb: main, variants = [] }: { kb: Kb; variants?: Kb[] }) {
  const all = useMemo(() => [main, ...variants.map((v) => withLines(v, main))], [main, variants]);
  const [language, setLanguage] = useState(languageOf(main));
  const kb = all.find((k) => languageOf(k) === language) ?? main;
  const source = useMemo(() => staticSource(kb), [kb]);
  const client = useMemo(() => new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } }), []);
  const hasGuide = kb.guide.length > 0;
  const tab = ({ isActive }: { isActive: boolean }) =>
    cn(
      "-mb-px border-b-2 px-1 pb-2.5 text-[15px] transition-colors duration-150",
      isActive ? "border-foreground text-foreground" : "border-transparent text-muted hover:text-foreground",
    );
  const guide = <GuidePanel source={source} base="" />;
  const code = <CodeViewer source={source} base="" />;
  return (
    <QueryClientProvider client={client}>
      <HashRouter>
        <div className="reading min-h-screen bg-background px-4 py-6 sm:px-8">
          <header>
            <h1 className="text-[34px] leading-tight font-medium tracking-[-0.02em]">{kb.project.name}</h1>
            <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[15px] text-muted">
              <span className="flex items-center gap-1.5 font-mono text-[13px]">
                <FolderGit2 aria-hidden="true" className="size-4" />
                {kb.project.repository}
              </span>
              <span className="font-mono text-[13px]">{kb.build.commit_sha.slice(0, 12)}</span>
              <span>Offline copy; explanations are AI-generated, so check the cited source.</span>
              {all.length > 1 ? (
                <label className="flex items-center gap-2">
                  Language
                  <select className="field h-8 py-0" value={language} onChange={(e) => setLanguage(e.target.value)}>
                    {all.map((k) => (
                      <option key={languageOf(k)} value={languageOf(k)} lang={languageOf(k)}>
                        {LANGUAGES[languageOf(k)] ?? languageOf(k)}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
            </p>
          </header>
          <nav aria-label="Sections" className="mt-5 flex gap-6 border-b border-border">
            {hasGuide ? (
              <NavLink end to="/" className={tab}>
                Guide
              </NavLink>
            ) : null}
            <NavLink to="/code" className={tab}>
              Code
            </NavLink>
            <NavLink to="/search" className={tab}>
              Search
            </NavLink>
          </nav>
          <main id="main" key={language}>
            <Routes>
              <Route path="/" element={hasGuide ? guide : code} />
              <Route path="/guide" element={guide} />
              <Route path="/guide/:slug" element={guide} />
              <Route path="/code" element={code} />
              <Route path="/search" element={<SearchPanel source={source} base="" />} />
            </Routes>
          </main>
        </div>
      </HashRouter>
    </QueryClientProvider>
  );
}
