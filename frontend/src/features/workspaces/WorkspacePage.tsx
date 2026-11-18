import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

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
  const create = useMutation({
    mutationFn: () => api.createProject(wid, { repository_id: repositoryId, name: name.trim(), slug: slug.trim() }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects", wid] }),
  });

  return (
    <section aria-labelledby="w-title" className="max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 id="w-title" className="text-2xl font-medium">Projects</h1>
        <Link to={`/w/${wid}/connections`} className="text-primary underline">GitHub connections</Link>{" "}
        · <Link to={`/w/${wid}/audit`} className="text-primary underline">Audit log</Link>
      </div>
      <ul className="mt-4 divide-y divide-border rounded-lg border border-border bg-surface">
        {projects.data?.items.map((p) => (
          <li key={p.id} className="flex items-center justify-between p-3">
            <Link to={`/p/${p.id}`} className="font-medium text-primary">{p.name}</Link>
            <span className="font-mono text-muted">{p.owner}/{p.repository}</span>
          </li>
        ))}
        {projects.data && projects.data.items.length === 0 ? <li className="p-3 text-muted">No projects yet.</li> : null}
      </ul>
      <h2 className="mt-8 text-lg font-medium">New project</h2>
      {active.length === 0 ? (
        <p className="mt-2 text-muted">Connect GitHub first to choose a repository.</p>
      ) : (
        <form
          className="mt-3 grid gap-3 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <label className="flex flex-col gap-1">
            <span>Connection</span>
            <select value={connectionId} onChange={(e) => setConnectionId(e.target.value)} required
              className="h-10 rounded-lg border border-input-border bg-surface px-3">
              <option value="">Choose…</option>
              {active.map((c) => <option key={c.id} value={c.id}>{c.account}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span>Repository</span>
            <select value={repositoryId} onChange={(e) => setRepositoryId(e.target.value)} required
              className="h-10 rounded-lg border border-input-border bg-surface px-3">
              <option value="">Choose…</option>
              {repos.data?.items.map((r) => <option key={r.id} value={r.id}>{r.owner}/{r.name}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span>Name</span>
            <input required maxLength={80} value={name} onChange={(e) => setName(e.target.value)}
              className="h-10 rounded-lg border border-input-border bg-surface px-3" />
          </label>
          <label className="flex flex-col gap-1">
            <span>Address</span>
            <input required pattern="[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?" value={slug} onChange={(e) => setSlug(e.target.value)}
              className="h-10 rounded-lg border border-input-border bg-surface px-3" />
          </label>
          <button type="submit" disabled={create.isPending}
            className="h-10 rounded-lg bg-primary px-4 font-medium text-primary-foreground sm:col-span-2">Create project</button>
          {create.error ? (
            <p role="alert" className="text-destructive sm:col-span-2">
              {create.error instanceof ApiError ? create.error.message : "Could not create the project."}
            </p>
          ) : null}
        </form>
      )}
    </section>
  );
}
