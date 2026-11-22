import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";

import { ConfirmDeletion } from "../../components/ConfirmDeletion";
import { CircleCheck, FileArchive, FolderGit2, GitBranch, Play, RefreshCw } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { EmptyState, PageHeader, Section } from "../../components/ui/section";
import { Skeleton } from "../../components/ui/skeleton";
import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";
import { GenerationPanel } from "./GenerationPanel";
import { StatusBadge } from "../viewer/StatusBadge";
import { PublicationPanel } from "./PublicationPanel";
import { AccessPanel } from "./AccessPanel";
import { AddressPanel } from "./AddressPanel";
import { PushPanel } from "./PushPanel";

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
  // The caller's role on this project: a project grant can differ from the workspace role (0027).
  const role = project.data?.my_role ?? workspaces.data?.items.find((w) => w.id === project.data?.workspace_id)?.role;
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

  const fresh = freshness.data;
  return (
    <section aria-labelledby="p-title" className="max-w-5xl">
      <PageHeader
        title={<span id="p-title">{project.data?.name ?? "Project"}</span>}
        meta={
          project.data ? (
            <>
              {project.data.source === "upload" ? (
                <span className="flex items-center gap-1.5 text-[13px]">
                  <FileArchive aria-hidden="true" className="size-4" />
                  Uploaded archive
                </span>
              ) : (
                <>
                  <span className="flex items-center gap-1.5 font-mono text-[13px]">
                    <FolderGit2 aria-hidden="true" className="size-4" />
                    {project.data.owner}/{project.data.repository}
                  </span>
                  <span className="flex items-center gap-1.5 font-mono text-[13px]">
                    <GitBranch aria-hidden="true" className="size-4" />
                    {project.data.tracked_ref}
                  </span>
                </>
              )}
              {fresh && fresh.state !== "not_tracked" ? (
                <Badge
                  role="status"
                  tone={fresh.state === "current" ? "success" : fresh.state === "update_available" ? "warning" : "neutral"}
                  icon={fresh.state === "current" ? <CircleCheck aria-hidden="true" /> : <RefreshCw aria-hidden="true" />}
                  title={FRESHNESS[fresh.state]}
                >
                  {FRESHNESS[fresh.state]}
                  {fresh.state === "unknown" && fresh.last_observed_at
                    ? ` Last seen ${fresh.last_observed_sha?.slice(0, 12)} at ${new Date(fresh.last_observed_at).toLocaleString()}.`
                    : null}
                  {fresh.state === "update_available" && fresh.latest_sha ? ` Latest commit ${fresh.latest_sha.slice(0, 12)}.` : null}
                </Badge>
              ) : null}
            </>
          ) : null
        }
        actions={
          <div className="flex flex-col items-end gap-1">
            <Button variant="primary" onClick={() => start.mutate()} disabled={start.isPending}>
              <Play aria-hidden="true" /> Free source-only build
            </Button>
            <span className="text-xs text-muted">Cards from docstrings and signatures; no model, no cost.</span>
          </div>
        }
      />
      {start.error ? (
        <p role="alert" className="mb-4 text-destructive">
          {start.error instanceof ApiError ? start.error.message : "Could not start the build."}
        </p>
      ) : null}
      <Section title="Builds" description="Each build reads one exact commit. Open a ready build to browse its code and guide.">
        {builds.data?.items.length ? (
          <div className="panel overflow-x-auto">
            <table className="w-full text-left">
              <caption className="sr-only">Builds</caption>
              <thead>
                <tr className="border-b border-border text-xs text-muted">
                  <th scope="col" className="px-4 py-2.5 font-medium">Commit</th>
                  <th scope="col" className="px-4 py-2.5 font-medium">Status</th>
                  <th scope="col" className="px-4 py-2.5 font-medium">Started</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {builds.data.items.map((b) => (
                  <tr key={b.id} className="hover:bg-surface-muted/50">
                    <td className="px-4 py-3">
                      <Link to={`/p/${pid}/builds/${b.id}`} className="font-mono text-[13px] font-medium hover:text-primary">
                        {b.commit_sha.slice(0, 12)}
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={b.status} />
                    </td>
                    <td className="px-4 py-3 text-muted">{new Date(b.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : builds.data ? (
          <EmptyState title="No builds yet">Start a free source-only build, or estimate one with explanations below.</EmptyState>
        ) : (
          <Skeleton className="h-24" />
        )}
      </Section>
      {project.data ? (
        <GenerationPanel projectId={pid} workspaceId={project.data.workspace_id} uploaded={project.data.source === "upload"} />
      ) : null}
      {project.data && (project.data.source ?? "github") === "github" ? (
        <PushPanel projectId={pid} trackedRef={project.data.tracked_ref} canChange={role === "admin" || role === "owner"} />
      ) : null}
      {project.data ? <PublicationPanel project={project.data} canPublish={role === "admin" || role === "owner"} /> : null}
      {project.data && (role === "admin" || role === "owner") ? (
        <AccessPanel projectId={pid} workspaceId={project.data.workspace_id} />
      ) : null}
      {project.data && (role === "admin" || role === "owner") ? <AddressPanel key={project.data.slug} project={project.data} /> : null}
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
