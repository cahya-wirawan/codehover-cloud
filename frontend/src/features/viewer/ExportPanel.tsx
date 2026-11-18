import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Braces, Download, FileArchive, Loader2 } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api, type ExportJob } from "../../lib/api/resources";

const FORMAT: Record<ExportJob["format"], string> = {
  static_zip: "Offline site (ZIP)",
  kb_json: "Knowledge base (JSON)",
};

/** Export a ready build (01 FR-018, 03 "Export"): the package contains source and explanations, so the
 * warning comes before the request; downloads are authorized on every request and expire after 24 h. */
export function ExportPanel({ buildId, canExport }: { buildId: string; canExport: boolean }) {
  const [job, setJob] = useState<ExportJob | null>(null);
  const status = useQuery({
    queryKey: ["export", job?.id],
    queryFn: () => api.exportStatus(job?.id ?? ""),
    enabled: job !== null,
    refetchInterval: (query) => (query.state.data && query.state.data.status !== "pending" ? false : 2000),
  });
  const start = useMutation({ mutationFn: (format: ExportJob["format"]) => api.requestExport(buildId, format), onSuccess: setJob });
  const current = status.data ?? job;
  if (!canExport) return null;

  return (
    <Section
      title="Export"
      description="Exports contain this build's source code and explanations. Downloaded copies cannot be recalled."
      actions={(Object.keys(FORMAT) as ExportJob["format"][]).map((format) => (
        <Button key={format} onClick={() => start.mutate(format)} disabled={start.isPending}>
          {format === "static_zip" ? <FileArchive aria-hidden="true" /> : <Braces aria-hidden="true" />}
          {FORMAT[format]}
        </Button>
      ))}
    >
      {start.error ? (
        <p role="alert" className="text-destructive">
          {start.error instanceof ApiError ? start.error.message : "Could not export."}
        </p>
      ) : null}
      {current ? (
        <p role="status" className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{FORMAT[current.format]}:</span>{" "}
          {current.status === "pending" ? (
            <Badge icon={<Loader2 aria-hidden="true" className="motion-safe:animate-spin" />}>being prepared…</Badge>
          ) : null}
          {current.status === "failed" ? <Badge tone="destructive">failed; try again.</Badge> : null}
          {current.status === "ready" && !current.expired ? (
            <Button asChild variant="primary" size="sm">
              <a href={api.exportDownloadUrl(current.id)}>
                <Download aria-hidden="true" /> Download
              </a>
            </Button>
          ) : null}
          {current.expired || current.status === "expired" ? <Badge>expired; request a new export.</Badge> : null}
          {current.status === "ready" && current.expires_at && !current.expired ? (
            <span className="text-muted">available until {new Date(current.expires_at).toLocaleString()}</span>
          ) : null}
        </p>
      ) : null}
    </Section>
  );
}
