import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

export function ProjectPage() {
  const { pid = "" } = useParams();
  const navigate = useNavigate();
  const project = useQuery({ queryKey: ["project", pid], queryFn: () => api.project(pid) });
  const builds = useQuery({ queryKey: ["builds", pid], queryFn: () => api.builds(pid) });
  const start = useMutation({
    mutationFn: () => api.startBuild(pid),
    onSuccess: (build) => navigate(`/p/${pid}/builds/${build.id}`),
  });

  return (
    <section aria-labelledby="p-title" className="max-w-3xl">
      <h1 id="p-title" className="text-2xl font-medium">{project.data?.name ?? "Project"}</h1>
      {project.data ? (
        <p className="mt-1 font-mono text-muted">
          {project.data.owner}/{project.data.repository} · {project.data.tracked_ref}
        </p>
      ) : null}
      <button type="button" onClick={() => start.mutate()} disabled={start.isPending}
        className="mt-4 h-10 rounded-lg bg-primary px-4 font-medium text-primary-foreground">
        Build documentation
      </button>
      {start.error ? (
        <p role="alert" className="mt-3 text-destructive">
          {start.error instanceof ApiError ? start.error.message : "Could not start the build."}
        </p>
      ) : null}
      <h2 className="mt-8 text-lg font-medium">Builds</h2>
      <ul className="mt-3 divide-y divide-border rounded-lg border border-border bg-surface">
        {builds.data?.items.map((b) => (
          <li key={b.id} className="flex items-center justify-between p-3">
            <Link to={`/p/${pid}/builds/${b.id}`} className="font-mono text-primary">{b.commit_sha.slice(0, 12)}</Link>
            <span className="text-muted">{b.status}</span>
          </li>
        ))}
        {builds.data && builds.data.items.length === 0 ? <li className="p-3 text-muted">No builds yet.</li> : null}
      </ul>
    </section>
  );
}
