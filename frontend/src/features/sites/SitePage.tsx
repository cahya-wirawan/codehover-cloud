import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useOutletContext, useParams } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api, siteSource, type ViewerSource } from "../../lib/api/resources";
import { CodeViewer } from "../viewer/CodeViewer";
import { GuidePanel } from "../viewer/GuidePanel";
import { SearchPanel } from "../viewer/SearchPanel";

interface SiteContext {
  source: ViewerSource;
  base: string;
}

const useSite = () => useOutletContext<SiteContext>();

/** A published site (02 S20): the same reading experience, served only through the publication
 * resolver. A withdrawn or private site the reader may not see is simply not found. */
export function SitePage() {
  const { site = "" } = useParams();
  const source = siteSource(site);
  const base = `/sites/${encodeURIComponent(site)}`;
  const info = useQuery({ queryKey: ["site", site], queryFn: () => api.site(site), retry: false });
  const tab = ({ isActive }: { isActive: boolean }) =>
    `rounded-lg px-3 py-1 ${isActive ? "bg-surface-muted font-medium" : "text-muted hover:text-foreground"}`;

  if (info.error) {
    return (
      <section aria-labelledby="site-title">
        <h1 id="site-title" className="text-2xl font-medium">Site not found</h1>
        <p className="mt-2 text-muted">
          {info.error instanceof ApiError && info.error.status === 404
            ? "This site does not exist, is no longer published, or is private to its workspace."
            : "The site could not be loaded."}
        </p>
      </section>
    );
  }
  return (
    <section aria-labelledby="site-title">
      <h1 id="site-title" className="text-2xl font-medium">{info.data?.project ?? "Loading…"}</h1>
      {info.data ? (
        <p className="mt-1 text-muted">
          {info.data.repository} · commit <span className="font-mono">{info.data.commit_sha.slice(0, 12)}</span> ·{" "}
          {info.data.visibility === "public" ? "public" : "private to its workspace"}
          {info.data.generated ? " · AI-generated explanations; check the cited source" : " · explanations derived from source only"}
        </p>
      ) : null}
      <nav aria-label="Site sections" className="mt-4 flex flex-wrap gap-1 border-b border-border pb-2">
        {info.data?.generated ? <NavLink end to={base} className={tab}>Guide</NavLink> : null}
        <NavLink to={`${base}/code`} className={tab}>Code</NavLink>
        <NavLink to={`${base}/search`} className={tab}>Search</NavLink>
      </nav>
      {info.data ? <Outlet context={{ source, base } satisfies SiteContext} /> : null}
    </section>
  );
}

export function SiteGuide() {
  const { source, base } = useSite();
  return <GuidePanel source={source} base={base} />;
}

export function SiteCode() {
  const { source } = useSite();
  return <CodeViewer source={source} />;
}

export function SiteSearch() {
  const { source, base } = useSite();
  return <SearchPanel source={source} base={base} />;
}
