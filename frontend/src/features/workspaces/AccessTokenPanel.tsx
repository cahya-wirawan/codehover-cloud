import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { Field, Input } from "../../components/ui/input";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api, type AccessToken } from "../../lib/api/resources";

const EXPIRY = [30, 90, 365] as const;

function state(token: AccessToken): string {
  if (token.revoked_at) return "Revoked";
  if (new Date(token.expires_at) <= new Date()) return "Expired";
  return `Expires ${new Date(token.expires_at).toLocaleDateString()}`;
}

/** Personal access tokens for the hosted MCP endpoint (06 "/mcp/{slug}"): each reads the private
 * published sites of this workspace that its creator may see, and nothing else. The secret is shown once. */
export function AccessTokenPanel({ workspaceId }: { workspaceId: string }) {
  const client = useQueryClient();
  const tokens = useQuery({ queryKey: ["access-tokens", workspaceId], queryFn: () => api.accessTokens(workspaceId) });
  const [name, setName] = useState("");
  const [days, setDays] = useState<number>(90);
  const [secret, setSecret] = useState<string | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: ["access-tokens", workspaceId] });
  const create = useMutation({
    mutationFn: () => api.createAccessToken(workspaceId, name.trim(), days),
    onSuccess: (created) => {
      setSecret(created.token);
      setName("");
      refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeAccessToken(workspaceId, id),
    onSuccess: refresh,
  });
  const failure = create.error ?? revoke.error;
  return (
    <Section
      title="Access tokens for MCP"
      description="An MCP client such as Claude Code reads this workspace's private published sites with a token. A token can only read, and only the sites you can see; it stops working when you revoke it or leave the workspace."
    >
      {secret ? (
        <div role="status" className="mb-4 rounded-md border border-border p-3">
          <p className="font-medium">Copy this token now. It is not shown again.</p>
          <code className="mt-2 block break-all font-mono text-[13px]">{secret}</code>
          <Button className="mt-2" size="sm" variant="secondary" onClick={() => setSecret(null)}>
            Done
          </Button>
        </div>
      ) : null}
      {tokens.data?.items.length ? (
        <ul className="mb-4 divide-y divide-border">
          {tokens.data.items.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3">
              <KeyRound aria-hidden="true" className="size-4 text-muted" />
              <span className="font-medium">{t.name}</span>
              <span className="font-mono text-[13px]">{t.prefix}…</span>
              <span className="text-muted">
                {state(t)}
                {t.last_used_at ? `, last used ${new Date(t.last_used_at).toLocaleDateString()}` : ", never used"}
              </span>
              {!t.revoked_at && new Date(t.expires_at) > new Date() ? (
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => revoke.mutate(t.id)}
                  disabled={revoke.isPending}
                  aria-label={`Revoke token ${t.name}`}
                >
                  Revoke
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-4 text-muted">You have no tokens in this workspace.</p>
      )}
      <form
        className="grid max-w-2xl gap-4 sm:grid-cols-[1fr_auto_auto] sm:items-end"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Token name">
          <Input value={name} maxLength={80} placeholder="Laptop, Claude Code" onChange={(e) => setName(e.target.value)} />
        </Field>
        <label className="flex flex-col gap-1.5 text-[13px] font-medium">
          Expires after
          <select className="field" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {EXPIRY.map((d) => (
              <option key={d} value={d}>
                {d} days
              </option>
            ))}
          </select>
        </label>
        <Button type="submit" disabled={create.isPending || !name.trim()}>
          Create token
        </Button>
      </form>
      {failure ? (
        <p role="alert" className="mt-3 text-destructive">
          {failure instanceof ApiError ? failure.message : "That did not work. Try again."}
        </p>
      ) : null}
    </Section>
  );
}
