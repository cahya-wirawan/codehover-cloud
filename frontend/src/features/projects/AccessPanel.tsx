import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock, Users } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { Field, Select } from "../../components/ui/input";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

/** Who can open this project (per-project access, 0027). Admins choose between the whole workspace and a
 * restricted list; admins and owners always have access, and a grant is the member's role here. */
export function AccessPanel({ projectId, workspaceId }: { projectId: string; workspaceId: string }) {
  const client = useQueryClient();
  const access = useQuery({ queryKey: ["project-access", projectId], queryFn: () => api.projectAccess(projectId) });
  const members = useQuery({ queryKey: ["members", workspaceId], queryFn: () => api.members(workspaceId) });
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState<"viewer" | "editor">("viewer");
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["project-access", projectId] });
    void client.invalidateQueries({ queryKey: ["projects", workspaceId] });
    void client.invalidateQueries({ queryKey: ["project", projectId] });
  };
  const mode = useMutation({
    mutationFn: (next: "workspace" | "restricted") => api.setProjectAccess(projectId, next, access.data?.etag ?? ""),
    onSuccess: refresh,
  });
  const add = useMutation({
    mutationFn: () => api.grantProjectAccess(projectId, userId, role),
    onSuccess: () => {
      setUserId("");
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: (id: string) => api.revokeProjectAccess(projectId, id), onSuccess: refresh });
  const current = access.data?.body;
  const granted = new Set(current?.members.map((m) => m.user_id));
  const candidates = members.data?.items.filter((m) => m.role !== "admin" && m.role !== "owner" && !granted.has(m.user_id)) ?? [];
  const failure = mode.error ?? add.error ?? remove.error;
  if (!current) return null;
  const restricted = current.mode === "restricted";
  return (
    <Section
      title="Access"
      description={
        restricted
          ? "Only workspace admins and owners, and the people listed here, can open this project and its site."
          : "Everyone in the workspace can open this project. Restrict it to choose who can."
      }
      actions={
        <Button onClick={() => mode.mutate(restricted ? "workspace" : "restricted")} disabled={mode.isPending}>
          {restricted ? <Users aria-hidden="true" /> : <Lock aria-hidden="true" />}
          {restricted ? "Open to the workspace" : "Restrict access"}
        </Button>
      }
    >
      {restricted ? (
        <>
          {current.members.length ? (
            <ul className="panel divide-y divide-border">
              {current.members.map((m) => (
                <li key={m.user_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
                  <span className="font-medium">{m.name}</span>
                  <span className="text-[13px] text-muted">
                    {m.role === "editor" ? "Can build" : "Can read"} · {m.workspace_role} in the workspace
                  </span>
                  <Button variant="ghost" size="sm" className="ml-auto" onClick={() => remove.mutate(m.user_id)} disabled={remove.isPending}>
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted">Nobody besides admins and owners yet.</p>
          )}
          <form
            className="mt-4 grid max-w-2xl gap-4 sm:grid-cols-[1fr_10rem_auto] sm:items-start"
            onSubmit={(event) => {
              event.preventDefault();
              add.mutate();
            }}
          >
            <Field label="Member">
              <Select value={userId} onChange={(e) => setUserId(e.target.value)}>
                <option value="">Choose…</option>
                {candidates.map((m) => (
                  <option key={m.user_id} value={m.user_id}>
                    {m.display_name} ({m.role})
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Can">
              <Select value={role} onChange={(e) => setRole(e.target.value as "viewer" | "editor")}>
                <option value="viewer">Read</option>
                <option value="editor">Build</option>
              </Select>
            </Field>
            <div className="flex flex-col gap-1.5">
              <span aria-hidden="true" className="hidden text-[13px] font-medium sm:block">
                &nbsp;
              </span>
              <Button type="submit" disabled={add.isPending || userId === ""}>
                Add
              </Button>
            </div>
          </form>
        </>
      ) : null}
      {failure ? (
        <p role="alert" className="mt-3 text-destructive">
          {failure instanceof ApiError ? failure.message : "Could not change access."}
        </p>
      ) : null}
    </Section>
  );
}
