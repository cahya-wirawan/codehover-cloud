import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { rememberOwner, type OwnerOption } from "../../app/ownerContext";
import { Button } from "../../components/ui/button";
import { Field, Select } from "../../components/ui/input";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api, type Workspace } from "../../lib/api/resources";

interface Clash {
  project_id: string;
  name: string;
  slug: string;
}

/** Moving a workspace to another owner (0042), for its owners: to themselves or to an organization they own
 * or administer. Its sites move to the new owner's address; old addresses keep leading there for 90 days. */
export function MoveWorkspacePanel({ workspace, current, options }: { workspace: Workspace; current: OwnerOption | null; options: OwnerOption[] }) {
  const client = useQueryClient();
  const targets = options.filter(
    (o) => o.handle !== current?.handle && (o.kind === "user" || o.role === "owner" || o.role === "admin"),
  );
  const [chosen, setChosen] = useState("");
  const target = targets.find((o) => o.handle === chosen) ?? null;
  const move = useMutation({
    mutationFn: () => api.moveWorkspace(workspace.id, target?.organizationId ?? null),
    onSuccess: async (moved) => {
      if (moved.owner) rememberOwner(moved.owner.handle);
      setChosen("");
      await client.invalidateQueries({ queryKey: ["workspaces"] });
      await client.invalidateQueries({ queryKey: ["billing", workspace.id] });
    },
  });
  if (!targets.length) return null;
  const error = move.error instanceof ApiError ? move.error : null;
  const clashes = error?.code === "SLUG_TAKEN" ? ((error.details.projects as Clash[] | undefined) ?? []) : [];
  return (
    <Section
      title="Move workspace"
      className="mt-10"
      description="Move this workspace, with its projects, members and published sites, to yourself or to an organization you own or administer. The new owner's plan applies from then on."
    >
      <form
        className="flex max-w-xl flex-col gap-4 sm:flex-row sm:items-end"
        onSubmit={(event) => {
          event.preventDefault();
          move.mutate();
        }}
      >
        <Field label="New owner" className="flex-1">
          <Select value={chosen} onChange={(e) => setChosen(e.target.value)}>
            <option value="">Choose…</option>
            {targets.map((o) => (
              <option key={o.handle} value={o.handle}>
                {o.kind === "user" ? `You (${o.handle})` : `${o.name} (${o.handle})`}
              </option>
            ))}
          </Select>
        </Field>
        <Button type="submit" variant="secondary" disabled={!target || move.isPending}>
          Move workspace
        </Button>
      </form>
      {target ? (
        <p className="mt-3 max-w-2xl text-muted">
          Its sites move to /sites/{target.handle}/…, and their current addresses keep leading there for 90 days.
          {target.kind === "organization" ? ` The owners and admins of ${target.name} get access to it.` : ""}
          {current?.kind === "organization" ? ` The owners and admins of ${current.name} lose the access it gave them.` : ""}
        </p>
      ) : null}
      {move.isSuccess ? (
        <p role="status" className="mt-3">
          Moved to {move.data.owner?.handle}.
        </p>
      ) : null}
      {error?.code === "BILLING_HELD" ? (
        <p role="alert" className="mt-3 text-destructive">
          {error.message} <Link to={`/w/${workspace.id}/usage`}>Plan and usage</Link>
        </p>
      ) : error ? (
        <div role="alert" className="mt-3 text-destructive">
          <p>{error.message}</p>
          {clashes.length ? (
            <ul className="mt-1 list-disc pl-5">
              {clashes.map((c) => (
                <li key={c.project_id}>
                  <Link to={`/p/${c.project_id}`}>{c.name}</Link> at <span className="font-mono">{c.slug}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : move.error ? (
        <p role="alert" className="mt-3 text-destructive">
          Could not move the workspace.
        </p>
      ) : null}
    </Section>
  );
}
