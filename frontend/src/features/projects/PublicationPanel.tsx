import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api, type Project } from "../../lib/api/resources";

const SUSPENDED: Record<string, string> = {
  connection_inactive: "The GitHub connection was revoked or suspended. Reconnect, then publish again.",
  repository_revoked: "Access to this repository was revoked. Reconnect, then publish again.",
  operator: "An operator suspended this site.",
  billing: "Sites are offline until the plan is in good standing.",
  license:
    "This public site was published before licenses were checked. Publish again: a public site of a public repository needs a license that allows sharing its code.",
};

function message(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/** Publish, switch, roll back or withdraw (02 S15). Private is the default; public needs an address and
 * the project name typed as confirmation. Only admins see the controls (09). */
export function PublicationPanel({ project, canPublish }: { project: Project; canPublish: boolean }) {
  const client = useQueryClient();
  const pub = useQuery({
    queryKey: ["publication", project.id],
    queryFn: async () => {
      try {
        return await api.publication(project.id);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
  });
  const builds = useQuery({ queryKey: ["builds", project.id], queryFn: () => api.builds(project.id) });
  const ready = builds.data?.items.filter((b) => b.status === "ready") ?? [];
  const current = pub.data?.body ?? null;
  const [buildId, setBuildId] = useState<string>("");
  const chosen = buildId || current?.build_id || ready[0]?.id || "";
  const [visibility, setVisibility] = useState<"private" | "public">("private");
  const [confirmation, setConfirmation] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const quality = useQuery({ queryKey: ["quality", chosen], queryFn: () => api.quality(chosen), enabled: chosen !== "" });
  const refresh = () => void client.invalidateQueries({ queryKey: ["publication", project.id] });
  const publish = useMutation({
    mutationFn: () =>
      api.publish(
        project.id,
        {
          build_id: chosen,
          visibility,
          confirmation: visibility === "public" ? confirmation : null,
          acknowledge_partial: acknowledged,
        },
        pub.data?.etag ?? null,
      ),
    onSuccess: refresh,
  });
  const unpublish = useMutation({
    mutationFn: () => api.unpublish(project.id, pub.data?.etag ?? ""),
    onSuccess: () => {
      setConfirmStop(false);
      refresh();
    },
  });
  const blocked = quality.data && !quality.data.publishable;
  const needsAck = quality.data?.requires_acknowledgment ?? false;

  return (
    <section aria-labelledby="pub-title" className="page-section">
      <h2 id="pub-title" className="section-title">Publication</h2>
      {current ? (
        <p className="mt-1">
          Published build <span className="font-mono">{current.commit_sha.slice(0, 12)}</span>,{" "}
          {current.visibility === "public" ? <strong>public</strong> : "private to this workspace"}
 at <Link className="text-primary underline" to={`/sites/${current.address}`}>/sites/{current.address}</Link>
          .
        </p>
      ) : (
        <p className="mt-1 text-muted">Not published. Every build stays private until an admin publishes it.</p>
      )}
      {current && !current.suspended_reason ? (
        <McpAddress address={current.address} isPublic={current.visibility === "public"} workspaceId={project.workspace_id} />
      ) : null}
      {current?.suspended_reason ? (
        <p role="alert" className="mt-2 text-destructive">{SUSPENDED[current.suspended_reason] ?? "Suspended."}</p>
      ) : null}

      {canPublish ? (
        ready.length ? (
          <form className="mt-4 space-y-3" onSubmit={(e) => { e.preventDefault(); publish.mutate(); }}>
            <label className="flex max-w-md flex-col gap-1">
              Build
              <select value={chosen} onChange={(e) => { setBuildId(e.target.value); setAcknowledged(false); }}
                className="field">
                {ready.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.commit_sha.slice(0, 12)} · {new Date(b.created_at).toLocaleString()}
                    {b.id === current?.build_id ? " (published)" : ""}
                  </option>
                ))}
              </select>
            </label>
            {blocked ? (
              <p role="alert" className="text-destructive">This build cannot be published yet: {quality.data?.blockers.join(", ")}. See its Review tab.</p>
            ) : null}
            <fieldset className="space-y-1">
              <legend className="font-medium">Who can read it</legend>
              <label className="flex items-center gap-2">
                <input type="radio" name="visibility" checked={visibility === "private"} onChange={() => setVisibility("private")} />
                Workspace members only
              </label>
              <label className="flex items-center gap-2">
                <input type="radio" name="visibility" checked={visibility === "public"} onChange={() => setVisibility("public")} />
                Anyone with the address (public)
              </label>
            </fieldset>
            <p className="text-muted">
              The site lives at <span className="font-mono">/sites/{current?.address ?? `…/${project.slug}`}</span>: the
              owner's name and this project's address.
            </p>
            {visibility === "public" ? (
              <div className="rounded-lg border border-input-border p-3">
                <p>
                  Public publication shows this project's selected source code and its explanations to anyone. Copies
                  others make cannot be recalled.
                </p>
                <label className="mt-2 flex max-w-md flex-col gap-1">
                  Type the project name <strong>{project.name}</strong> to confirm
                  <input value={confirmation} onChange={(e) => setConfirmation(e.target.value)}
                    className="field" />
                </label>
              </div>
            ) : null}
            {needsAck ? (
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
                I reviewed the partly supported claims; they stay labeled on the site.
              </label>
            ) : null}
            <button type="submit"
              disabled={publish.isPending || !chosen || Boolean(blocked) || (needsAck && !acknowledged) || (visibility === "public" && confirmation !== project.name)}
              className="btn btn-primary btn-lg">
              {current ? "Update publication" : "Publish"}
            </button>
            {publish.error ? <p role="alert" className="text-destructive">{message(publish.error, "Could not publish.")}</p> : null}
          </form>
        ) : (
          <p className="mt-3 text-muted">Publish becomes available when a build is ready.</p>
        )
      ) : (
        <p className="mt-3 text-sm text-muted">Only workspace admins can publish.</p>
      )}

      {canPublish && current ? (
        <div className="mt-4">
          {confirmStop ? (
            <div className="flex flex-wrap items-center gap-2">
              <span>Stop serving this site now? Readers lose access immediately.</span>
              <button type="button" onClick={() => unpublish.mutate()} disabled={unpublish.isPending}
                className="btn btn-danger">Stop publishing {project.name}</button>
              <button type="button" onClick={() => setConfirmStop(false)} className="btn">Keep it</button>
            </div>
          ) : (
            <button type="button" onClick={() => setConfirmStop(true)} className="btn">
              Stop publishing…
            </button>
          )}
          {unpublish.error ? <p role="alert" className="mt-2 text-destructive">{message(unpublish.error, "Could not unpublish.")}</p> : null}
        </div>
      ) : null}
    </section>
  );
}

/** The site's hosted MCP address (06 "/mcp/<owner>/<project>") and the command that adds it to Claude Code. */
function McpAddress({ address, isPublic, workspaceId }: { address: string; isPublic: boolean; workspaceId: string }) {
  const url = `${window.location.origin}/api/v1/mcp/${address}`;
  const name = address.replace("/", "-");
  const command = `claude mcp add --transport http ${name} ${url}`;
  return (
    <div className="mt-3">
      <p>
        MCP clients can read this site at <code className="font-mono text-[13px]">{url}</code>.
        {isPublic ? null : (
          <>
            {" "}The client asks you to sign in through the browser; clients without sign-in can send an{" "}
            <Link className="text-primary underline" to={`/w/${workspaceId}`}>
              access token
            </Link>{" "}
            instead.
          </>
        )}
      </p>
      <pre
        tabIndex={0}
        aria-label="Command to add this site to Claude Code"
        className="mt-2 overflow-x-auto rounded-md border border-border p-2 font-mono text-[13px]"
      >
        {command}
      </pre>
    </div>
  );
}
