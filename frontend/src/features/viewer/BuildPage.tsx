import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router";

import { api, TERMINAL } from "../../lib/api/resources";
import { CodeViewer } from "./CodeViewer";

const STATE_LABEL: Record<string, string> = {
  pending: "Pending",
  leased: "Running",
  succeeded: "Done",
  skipped: "Reused",
  retry_wait: "Waiting to retry",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function BuildPage() {
  const { bid = "" } = useParams();
  const build = useQuery({
    queryKey: ["build", bid],
    queryFn: () => api.build(bid),
    // Polling fallback (02 S09); counts, not fake progress.
    refetchInterval: (query) => (query.state.data && TERMINAL.has(query.state.data.status) ? false : 2000),
  });
  const data = build.data;

  return (
    <section aria-labelledby="b-title">
      <h1 id="b-title" className="text-2xl font-medium">Build</h1>
      {data ? (
        <>
          <p className="mt-1 text-muted">
            Commit <span className="font-mono">{data.commit_sha.slice(0, 12)}</span> · status{" "}
            <strong className={data.status === "failed" ? "text-destructive" : ""}>{data.status}</strong>
            {data.rejection_code ? <> · rejected by safety scan ({data.rejection_code})</> : null}
          </p>
          <ol className="mt-4 flex flex-wrap gap-2" aria-label="Stages">
            {data.stages.map((s) => (
              <li key={s.stage} className="rounded-lg border border-border bg-surface px-3 py-1">
                {s.stage}: {STATE_LABEL[s.state] ?? s.state}
                {s.error_code ? <span className="text-destructive"> ({s.error_code})</span> : null}
              </li>
            ))}
          </ol>
          {data.status === "ready" ? (
            <>
              <p className="mt-4 text-muted">
                {data.counts.files} files · {data.counts.symbols} definitions · explanations are derived from source only
                (replay); check cited code.
              </p>
              <CodeViewer buildId={bid} />
            </>
          ) : null}
        </>
      ) : (
        <p role="status" className="mt-3 text-muted">Loading build…</p>
      )}
    </section>
  );
}
