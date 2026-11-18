import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";

import { ConfirmDeletion } from "../../components/ConfirmDeletion";
import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";
import { GenerationPanel } from "./GenerationPanel";
import { PublicationPanel } from "./PublicationPanel";

const FRESHNESS: Record<string, string> = {
  current: "Up to date with the tracked branch.",
  update_available: "Update available: the tracked branch has moved since the last build.",
  never_built: "Not built yet.",
  unknown: "Freshness unknown: GitHub could not be reached.",
};

export function ProjectPage() {
  const { pid = "" } = useParams();
  const navigate = useNavigate();
  const project = useQuery({ queryKey: ["project", pid], queryFn: () => api.project(pid) });
  const builds = useQuery({ queryKey: ["builds", pid], queryFn: () => api.builds(pid) });
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const freshness = useQuery({ queryKey: ["freshness", pid], queryFn: () => api.freshness(pid), retry: false });
  const role = workspaces.data?.items.find((w) => w.id === project.data?.workspace_id)?.role;
  const start = useMutation({
    mutationFn: () => api.startBuild(pid),
    onSuccess: (build) => navigate(`/p/${pid}/builds/${build.id}`),
  });
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.deleteProject(pid, project.data?.name ?? ""),
    onSuccess: async () => {
      const wid = project.data?.workspace_id;
      queryClient.removeQueries({ queryKey: ["project", pid] });
      await queryClient.invalidateQueries({ queryKey: ["projects", wid] });
      await navigate(`/w/${wid}`, { replace: true });
    },
  });

  return (
    <section aria-labelledby="p-title" className="max-w-3xl">
      <h1 id="p-title" className="text-2xl font-medium">{project.data?.name ?? "Project"}</h1>
      {freshness.data ? (
        <p className="mt-1" role="status">
          {FRESHNESS[freshness.data.state]}
          {freshness.data.state === "unknown" && freshness.data.last_observed_at
            ? ` Last seen ${freshness.data.last_observed_sha?.slice(0, 12)} at ${new Date(freshness.data.last_observed_at).toLocaleString()}.`
            : null}
          {freshness.data.state === "update_available" && freshness.data.latest_sha
            ? ` Latest commit ${freshness.data.latest_sha.slice(0, 12)}.`
            : null}
        </p>
      ) : null}
      {project.data ? (
        <p className="mt-1 font-mono text-muted">
          {project.data.owner}/{project.data.repository} · {project.data.tracked_ref}
        </p>
      ) : null}
      <button type="button" onClick={() => start.mutate()} disabled={start.isPending}
        className="mt-4 h-10 rounded-lg border border-border px-4 font-medium">
        Free source-only build
      </button>
      <p className="mt-1 text-sm text-muted">Cards from docstrings and signatures; no model, no cost.</p>
      {start.error ? (
        <p role="alert" className="mt-3 text-destructive">
          {start.error instanceof ApiError ? start.error.message : "Could not start the build."}
        </p>
      ) : null}
      {project.data ? <GenerationPanel projectId={pid} workspaceId={project.data.workspace_id} /> : null}
      {project.data ? <PublicationPanel project={project.data} canPublish={role === "admin" || role === "owner"} /> : null}
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
      {project.data && (role === "admin" || role === "owner") ? (
        <ConfirmDeletion
          title="Delete project"
          expected={project.data.name}
          expectedLabel="the project name"
          actionLabel={`Delete ${project.data.name}`}
          pending={remove.isPending}
          error={remove.error}
          onConfirm={() => remove.mutate()}
        >
          <p>
            Deleting stops its site immediately, cancels running builds and removes its source, explanations, guide
            and exports within 24 hours. Downloaded exports cannot be recalled.
          </p>
        </ConfirmDeletion>
      ) : null}
    </section>
  );
}
