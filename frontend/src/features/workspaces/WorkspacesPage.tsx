import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { Button } from "../../components/ui/button";
import { Field, Input } from "../../components/ui/input";
import { EmptyState, PageHeader, Section } from "../../components/ui/section";
import { Skeleton } from "../../components/ui/skeleton";
import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

export function WorkspacesPage() {
  const queryClient = useQueryClient();
  const { data, isPending } = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const create = useMutation({
    mutationFn: () => api.createWorkspace(name.trim(), slug.trim()),
    onSuccess: async () => {
      setName("");
      setSlug("");
      await queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });

  return (
    <section aria-labelledby="ws-title" className="max-w-3xl">
      <PageHeader
        title={<span id="ws-title">Workspaces</span>}
        description="A workspace holds projects and the people who can read them. Everyone in a workspace sees its projects, unless an admin restricts a project to chosen members."
      />
      {isPending ? (
        <div className="flex flex-col gap-2" aria-hidden="true">
          <Skeleton className="h-14" />
          <Skeleton className="h-14" />
        </div>
      ) : null}
      {data && data.items.length === 0 ? (
        <EmptyState title="No workspaces yet">Create your first workspace below, then connect a repository to it.</EmptyState>
      ) : null}
      {data?.items.length ? (
        <ul className="panel divide-y divide-border">
          {data.items.map((w) => (
            <li key={w.id}>
              <Link to={`/w/${w.id}`} className="group flex items-center gap-3 px-4 py-3 hover:bg-surface-muted/60">
                <span aria-hidden="true" className="grid size-8 place-items-center rounded-[6px] bg-surface-muted font-semibold">
                  {w.name.slice(0, 1).toUpperCase()}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{w.name}</span>
                  <span className="block truncate font-mono text-xs text-muted">{w.slug}</span>
                </span>
                <span className="text-xs text-muted capitalize">{w.role}</span>
                <ChevronRight aria-hidden="true" className="size-4 text-muted group-hover:text-foreground" />
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
      <Section title="New workspace" className="mt-10">
        <form
          className="grid gap-4 sm:grid-cols-[1fr_1fr_auto] sm:items-start"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <Field label="Name">
            <Input required minLength={2} maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Address" hint="Lowercase letters, digits and hyphens; used in links.">
            <Input
              required
              pattern="[a-z0-9][a-z0-9-]{1,46}[a-z0-9]"
              value={slug}
              onChange={(e) => setSlug(e.target.value.toLowerCase())}
              placeholder="my-team"
            />
          </Field>
          {/* An empty label row puts the button on the inputs' line; the Address hint hangs below. */}
          <div className="flex flex-col gap-1.5">
            <span aria-hidden="true" className="hidden text-[13px] font-medium sm:block">
              &nbsp;
            </span>
            <Button type="submit" variant="primary" disabled={create.isPending}>
              Create workspace
            </Button>
          </div>
        </form>
        {create.error ? (
          <p role="alert" className="mt-3 text-destructive">
            {create.error instanceof ApiError ? create.error.message : "Could not create the workspace."}
          </p>
        ) : null}
      </Section>
    </section>
  );
}
