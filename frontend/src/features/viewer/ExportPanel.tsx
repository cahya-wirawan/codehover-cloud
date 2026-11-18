import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

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
    <section aria-labelledby="export-title" className="mt-6 rounded-lg border border-border bg-surface p-4">
      <h2 id="export-title" className="font-medium">Export</h2>
      <p className="mt-1 text-sm text-muted">
        Exports contain this build's source code and explanations. Downloaded copies cannot be recalled.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {(Object.keys(FORMAT) as ExportJob["format"][]).map((format) => (
          <button key={format} type="button" onClick={() => start.mutate(format)} disabled={start.isPending}
            className="h-9 rounded-lg border border-input-border px-3">
            {FORMAT[format]}
          </button>
        ))}
      </div>
      {start.error ? (
        <p role="alert" className="mt-2 text-destructive">{start.error instanceof ApiError ? start.error.message : "Could not export."}</p>
      ) : null}
      {current ? (
        <p role="status" className="mt-3">
          {FORMAT[current.format]}:{" "}
          {current.status === "pending" ? "being prepared…" : null}
          {current.status === "failed" ? "failed; try again." : null}
          {current.status === "ready" && !current.expired ? (
            <a className="text-primary underline" href={api.exportDownloadUrl(current.id)}>Download</a>
          ) : null}
          {current.expired || current.status === "expired" ? "expired; request a new export." : null}
          {current.status === "ready" && current.expires_at && !current.expired
            ? ` (available until ${new Date(current.expires_at).toLocaleString()})`
            : null}
        </p>
      ) : null}
    </section>
  );
}
