import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitCommitHorizontal } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Field, Input, Select } from "../../components/ui/input";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api, formatUsd, type PushPolicyName, type PushRequest } from "../../lib/api/resources";

const POLICIES: [PushPolicyName, string][] = [
  ["off", "Do nothing"],
  ["snapshot", "Take a snapshot to estimate later"],
  ["build", "Build the new commit"],
];

const OUTCOMES: Record<string, string> = {
  snapshot_requested: "Snapshot taken",
  build_started: "Build started",
  acquiring: "Taking the snapshot",
  already_built: "Already built",
  over_push_cap: "Estimate above the cap: waiting for approval",
  policy_off: "Rebuilding was turned off",
  policy_admin_lost: "Stopped: the admin who chose this is no longer an admin",
  snapshot_rejected: "The commit failed the safety scan",
  waiting_for_previous: "Waiting for the previous push",
};

function outcome(request: PushRequest): string {
  if (request.outcome && OUTCOMES[request.outcome]) return OUTCOMES[request.outcome] ?? "";
  if (request.state === "pending") return "Waiting for more pushes";
  return (request.outcome ?? request.state).replaceAll("_", " ");
}

/** What a push to the tracked branch does (01 FR-025). Admins choose; work runs on their behalf. Nothing a
 * push starts is published. */
export function PushPanel({ projectId, trackedRef, canChange }: { projectId: string; trackedRef: string; canChange: boolean }) {
  const client = useQueryClient();
  const policy = useQuery({ queryKey: ["push-policy", projectId], queryFn: () => api.pushPolicy(projectId) });
  const history = useQuery({ queryKey: ["push-requests", projectId], queryFn: () => api.pushRequests(projectId) });
  const [choice, setChoice] = useState<PushPolicyName | null>(null);
  const [cap, setCap] = useState<string | null>(null);
  const current = policy.data?.body;
  const selected = choice ?? current?.policy ?? "off";
  const capDollars = cap ?? (current ? (current.cap_micro_usd / 1_000_000).toFixed(2) : "0.00");
  const save = useMutation({
    mutationFn: () =>
      api.setPushPolicy(projectId, selected, Math.round(Number(capDollars) * 1_000_000), policy.data?.etag ?? ""),
    onSuccess: () => {
      setChoice(null);
      setCap(null);
      void client.invalidateQueries({ queryKey: ["push-policy", projectId] });
    },
  });
  const capValid = /^\d{1,4}(\.\d{1,2})?$/.test(capDollars);
  return (
    <Section
      title="Rebuild on push"
      description={
        <>
          When someone pushes to <span className="font-mono text-[13px]">{trackedRef}</span>. Pushes within a minute
          count as one, and a commit is never built twice. Nothing a push starts is published.
        </>
      }
    >
      {current ? (
        <form
          className="grid max-w-3xl gap-4 sm:grid-cols-[1fr_12rem_auto] sm:items-start"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <Field label="On push">
            <Select value={selected} disabled={!canChange} onChange={(e) => setChoice(e.target.value as PushPolicyName)}>
              {POLICIES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Build cap (USD)" hint="Builds start only if the estimate fits.">
            <Input
              inputMode="decimal"
              value={capDollars}
              disabled={!canChange || selected !== "build"}
              onChange={(e) => setCap(e.target.value)}
            />
          </Field>
          {canChange ? (
            <div className="flex flex-col gap-1.5">
              <span aria-hidden="true" className="hidden text-[13px] font-medium sm:block">
                &nbsp;
              </span>
              <Button type="submit" disabled={save.isPending || !capValid}>
                Save
              </Button>
            </div>
          ) : null}
        </form>
      ) : null}
      {current && current.policy !== "off" && current.set_by_name ? (
        <p className="mt-3 text-[13px] text-muted">
          Chosen by {current.set_by_name}; push work runs on their behalf
          {current.policy === "build" ? ` and spends up to ${formatUsd(current.cap_micro_usd)} per build` : ""}.
        </p>
      ) : null}
      {!canChange ? <p className="mt-3 text-[13px] text-muted">Only workspace admins can change this.</p> : null}
      {save.error ? (
        <p role="alert" className="mt-3 text-destructive">
          {save.error instanceof ApiError ? save.error.message : "Could not save the push setting."}
        </p>
      ) : null}
      {history.data?.items.length ? (
        <ul className="panel mt-5 divide-y divide-border">
          {history.data.items.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
              <GitCommitHorizontal aria-hidden="true" className="size-4 text-muted" />
              <span className="font-mono text-[13px]">{r.commit_sha.slice(0, 12)}</span>
              {r.pushes > 1 ? <Badge>{r.pushes} pushes</Badge> : null}
              <span className="text-muted">{outcome(r)}</span>
              {r.build_id ? (
                <Link to={`/p/${projectId}/builds/${r.build_id}`} className="text-primary underline underline-offset-2">
                  Open build
                </Link>
              ) : null}
              <span className="ml-auto text-xs text-muted">{new Date(r.first_received_at).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </Section>
  );
}
