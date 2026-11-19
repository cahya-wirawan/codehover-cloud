import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { FolderGit2, GitBranch, Lock, Plus } from "lucide-react";

import { ConfirmDeletion } from "../../components/ConfirmDeletion";
import { Button } from "../../components/ui/button";
import { Field, Input, Select } from "../../components/ui/input";
import { EmptyState, PageHeader, Section } from "../../components/ui/section";
import { Skeleton } from "../../components/ui/skeleton";
import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";
import { SupportAccessPanel } from "./SupportAccessPanel";

export function WorkspacePage() {
  const { wid = "" } = useParams();
  const queryClient = useQueryClient();
  const projects = useQuery({ queryKey: ["projects", wid], queryFn: () => api.projects(wid) });
  const connections = useQuery({ queryKey: ["connections", wid], queryFn: () => api.connections(wid), retry: false });
  const active = connections.data?.items.filter((c) => c.status === "active") ?? [];
  const [connectionId, setConnectionId] = useState("");
  const repos = useQuery({
    queryKey: ["repositories", wid, connectionId],
    queryFn: () => api.repositories(wid, connectionId),
    enabled: connectionId !== "",
  });
  const [repositoryId, setRepositoryId] = useState("");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const navigate = useNavigate();
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const workspace = workspaces.data?.items.find((w) => w.id === wid);
  const remove = useMutation({
    mutationFn: () => api.deleteWorkspace(wid, workspace?.slug ?? ""),
    onSuccess: async () => {
      queryClient.clear();
      await navigate("/w", { replace: true });
    },
  });
  const [source, setSource] = useState<"github" | "public" | "upload" | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [publicRepository, setPublicRepository] = useState("");
  const [branch, setBranch] = useState("");
  const chosenSource = source ?? (active.length ? "github" : "public");
  const create = useMutation({
    mutationFn: async () => {
      if (chosenSource === "github") {
        return api.createProject(wid, { repository_id: repositoryId, name: name.trim(), slug: slug.trim() });
      }
      if (chosenSource === "public") {
        return api.createProject(wid, {
          source: "public",
          public_repository: publicRepository.trim(),
          tracked_ref: branch.trim(),
          name: name.trim(),
          slug: slug.trim(),
        });
      }
      const project = await api.createProject(wid, { source: "upload", name: name.trim(), slug: slug.trim() });
      if (file) await api.uploadArchive(project.id, file);
      return project;
    },
    onSuccess: async (project) => {
      await queryClient.invalidateQueries({ queryKey: ["projects", wid] });
      if (chosenSource !== "github") await navigate(`/p/${project.id}`);
    },
  });

  return (
    <section aria-labelledby="w-title" className="max-w-5xl">
      <PageHeader
        title={<span id="w-title">Projects</span>}
        description={workspace ? `Everything ${workspace.name} has built from its repositories.` : undefined}
      />
      {projects.isPending ? (
        <div className="flex flex-col gap-2" aria-hidden="true">
          <Skeleton className="h-12" />
          <Skeleton className="h-12" />
        </div>
      ) : projects.data?.items.length ? (
        <div className="panel overflow-x-auto">
          <table className="w-full text-left">
            <caption className="sr-only">Projects</caption>
            <thead>
              <tr className="border-b border-border text-xs text-muted">
                <th scope="col" className="px-4 py-2.5 font-medium">Project</th>
                <th scope="col" className="px-4 py-2.5 font-medium">Repository</th>
                <th scope="col" className="px-4 py-2.5 font-medium">Tracked ref</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {projects.data.items.map((p) => (
                <tr key={p.id} className="hover:bg-surface-muted/50">
                  <td className="px-4 py-3">
                    <Link to={`/p/${p.id}`} className="font-medium hover:text-primary">
                      {p.name}
                    </Link>
                    {p.access === "restricted" ? (
                      <span className="ml-2 inline-flex items-center gap-1 text-xs text-muted" title="Only chosen members can open this project">
                        <Lock aria-hidden="true" className="size-3.5" />
                        Restricted
                      </span>
                    ) : null}
                  </td>
                  <td className="px-4 py-3">
                    <span className="flex items-center gap-1.5 font-mono text-[13px] text-muted">
                      <FolderGit2 aria-hidden="true" className="size-4" />
                      {p.owner}/{p.repository}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="flex items-center gap-1.5 font-mono text-[13px] text-muted">
                      <GitBranch aria-hidden="true" className="size-4" />
                      {p.tracked_ref}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState title="No projects yet">
          {active.length
            ? "Create one below from a connected repository."
            : "Create one below from a public GitHub repository or a ZIP archive, or connect GitHub for private repositories."}
        </EmptyState>
      )}

      <Section
        title="New project"
        description="A project follows one GitHub branch or an uploaded archive; each build pins one exact version."
        className="mt-10"
      >
        <form
          className="grid gap-4 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <fieldset className="flex flex-wrap gap-x-5 gap-y-2 sm:col-span-2">
            <legend className="mb-1.5 text-[13px] font-medium">Source</legend>
            <label className="flex items-center gap-2">
              <input type="radio" name="source" checked={chosenSource === "github"} onChange={() => setSource("github")} />
              A connected GitHub repository
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="source" checked={chosenSource === "public"} onChange={() => setSource("public")} />
              A public GitHub repository
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="source" checked={chosenSource === "upload"} onChange={() => setSource("upload")} />
              An uploaded ZIP archive
            </label>
          </fieldset>
          {chosenSource === "github" && active.length === 0 ? (
            <p className="text-muted sm:col-span-2">
              <Link to={`/w/${wid}/connections`} className="text-primary underline underline-offset-2">
                Connect GitHub
              </Link>{" "}
              to choose a repository.
            </p>
          ) : null}
          {chosenSource === "github" && active.length ? (
            <>
              <Field label="Connection">
                <Select value={connectionId} onChange={(e) => setConnectionId(e.target.value)} required>
                  <option value="">Choose…</option>
                  {active.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.account}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Repository">
                <Select value={repositoryId} onChange={(e) => setRepositoryId(e.target.value)} required>
                  <option value="">Choose…</option>
                  {repos.data?.items.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.owner}/{r.name}
                    </option>
                  ))}
                </Select>
              </Field>
            </>
          ) : null}
          {chosenSource === "public" ? (
            <>
              <Field label="Public repository" hint="owner/name or its github.com address. No GitHub connection needed.">
                <Input
                  required
                  maxLength={200}
                  placeholder="octocat/Hello-World"
                  value={publicRepository}
                  onChange={(e) => setPublicRepository(e.target.value)}
                  className="font-mono text-[13px]"
                />
              </Field>
              <Field label="Branch" hint="Leave empty for the default branch. Builds start by hand; there are no push rebuilds.">
                <Input maxLength={255} value={branch} onChange={(e) => setBranch(e.target.value)} className="font-mono text-[13px]" />
              </Field>
            </>
          ) : null}
          {chosenSource === "upload" ? (
            <Field label="Archive (ZIP)" hint="At most 100 MiB. You can upload new versions later." className="sm:col-span-2">
              <Input type="file" accept=".zip,application/zip" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            </Field>
          ) : null}
            <Field label="Name">
              <Input required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label="Address" hint="Lowercase letters, digits and hyphens.">
              <Input
                required
                pattern="[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?"
                value={slug}
                onChange={(e) => setSlug(e.target.value.toLowerCase())}
              />
            </Field>
            <div className="sm:col-span-2">
              <Button
                type="submit"
                variant="primary"
                disabled={
                  create.isPending ||
                  (chosenSource === "github" ? active.length === 0 : chosenSource === "upload" ? file === null : !publicRepository.trim())
                }
              >
                <Plus aria-hidden="true" /> Create project
              </Button>
            </div>
            {create.error ? (
              <p role="alert" className="text-destructive sm:col-span-2">
                {create.error instanceof ApiError ? create.error.message : "Could not create the project."}
              </p>
            ) : null}
        </form>
      </Section>
      {workspace?.role === "admin" || workspace?.role === "owner" ? <SupportAccessPanel workspaceId={wid} /> : null}
      {workspace?.role === "owner" ? (
        <ConfirmDeletion
          title="Delete workspace"
          expected={workspace.slug}
          expectedLabel="the workspace address"
          actionLabel={`Delete ${workspace.name}`}
          pending={remove.isPending}
          error={remove.error}
          onConfirm={() => remove.mutate()}
        >
          <p>
            Deleting removes every project, site and member access immediately; source and explanations are purged
            within 24 hours. Downloaded exports cannot be recalled.
          </p>
        </ConfirmDeletion>
      ) : null}
    </section>
  );
}
