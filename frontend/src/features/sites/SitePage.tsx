import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useOutletContext, useParams } from "react-router";

import { FolderGit2, Globe, Lock } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { PageHeader } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { cn } from "../../lib/cn";
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
    cn(
      "-mb-px border-b-2 px-1 pb-2.5 text-[13px] font-medium transition-colors duration-150",
      isActive ? "border-primary text-foreground" : "border-transparent text-muted hover:text-foreground",
    );

  if (info.error) {
    return (
      <section aria-labelledby="site-title" className="mx-auto mt-16 max-w-md text-center">
        <h1 id="site-title" className="text-[22px] leading-8 font-semibold">Site not found</h1>
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
      <PageHeader
        title={<span id="site-title">{info.data?.project ?? "Loading…"}</span>}
        meta={
          info.data ? (
            <>
              <span className="flex items-center gap-1.5 font-mono text-[13px]">
                <FolderGit2 aria-hidden="true" className="size-4" />
                {info.data.repository}
              </span>
              <span className="font-mono text-[13px]">{info.data.commit_sha.slice(0, 12)}</span>
              {info.data.visibility === "public" ? (
                <Badge tone="primary" icon={<Globe aria-hidden="true" />}>public</Badge>
              ) : (
                <Badge icon={<Lock aria-hidden="true" />}>private to its workspace</Badge>
              )}
              <span className="text-[13px]">
                {info.data.generated ? "AI-generated explanations; check the cited source" : "Explanations derived from source only"}
              </span>
            </>
          ) : null
        }
      />
      <nav aria-label="Site sections" className="flex flex-wrap gap-6 border-b border-border">
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
