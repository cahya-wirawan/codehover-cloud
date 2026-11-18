import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

export function WorkspacesPage() {
  const queryClient = useQueryClient();
  const { data, isPending } = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const create = useMutation({
    mutationFn: () => api.createWorkspace(name.trim(), slug.trim()),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workspaces"] }),
  });

  return (
    <section aria-labelledby="ws-title" className="max-w-2xl">
      <h1 id="ws-title" className="text-2xl font-medium">Workspaces</h1>
      <p className="mt-2 text-muted">Everyone in a workspace can see all of its projects.</p>
      {isPending ? <p role="status" className="mt-4 text-muted">Loading…</p> : null}
      <ul className="mt-4 divide-y divide-border rounded-lg border border-border bg-surface">
        {data?.items.map((w) => (
          <li key={w.id} className="flex items-center justify-between p-3">
            <Link to={`/w/${w.id}`} className="font-medium text-primary">{w.name}</Link>
            <span className="text-muted">{w.role}</span>
          </li>
        ))}
        {data && data.items.length === 0 ? <li className="p-3 text-muted">No workspaces yet. Create one below.</li> : null}
      </ul>
      <form
        className="mt-6 flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <label className="flex flex-col gap-1">
          <span>Name</span>
          <input required minLength={2} maxLength={80} value={name} onChange={(e) => setName(e.target.value)}
            className="h-10 rounded-lg border border-input-border bg-surface px-3" />
        </label>
        <label className="flex flex-col gap-1">
          <span>Address</span>
          <input required pattern="[a-z0-9][a-z0-9-]{1,46}[a-z0-9]" value={slug} onChange={(e) => setSlug(e.target.value)}
            className="h-10 rounded-lg border border-input-border bg-surface px-3" />
        </label>
        <button type="submit" disabled={create.isPending} className="h-10 rounded-lg bg-primary px-4 font-medium text-primary-foreground">
          Create workspace
        </button>
      </form>
      {create.error ? (
        <p role="alert" className="mt-3 text-destructive">
          {create.error instanceof ApiError ? create.error.message : "Could not create the workspace."}
        </p>
      ) : null}
    </section>
  );
}
