import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { Field, Input } from "../../components/ui/input";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api, type KeyProvider, type ProviderKey } from "../../lib/api/resources";
import { KEY_PROVIDERS } from "./providers";

/** A workspace's own model provider keys (M6, T24; 07 BYOK): one per provider, stored sealed, shown only
 * as a hint, used for builds that choose them. Owners manage them; each key is checked with its provider
 * before it is saved. */
export function ProviderKeyPanel({ workspaceId, owner }: { workspaceId: string; owner: boolean }) {
  const keys = useQuery({ queryKey: ["provider-keys", workspaceId], queryFn: () => api.providerKeys(workspaceId) });
  return (
    <Section
      title="Your own model provider keys"
      description="Builds that choose your own key are billed by that provider to your account and do not use the plan allowance. Keys are stored encrypted and never shown again."
    >
      <ul className="divide-y divide-border">
        {KEY_PROVIDERS.map((p) => (
          <ProviderKeyRow
            key={p.id}
            workspaceId={workspaceId}
            owner={owner}
            provider={p.id}
            name={p.name}
            current={keys.data?.items.find((k) => k.provider === p.id)}
          />
        ))}
      </ul>
    </Section>
  );
}

function ProviderKeyRow({
  workspaceId,
  owner,
  provider,
  name,
  current,
}: {
  workspaceId: string;
  owner: boolean;
  provider: KeyProvider;
  name: string;
  current: ProviderKey | undefined;
}) {
  const client = useQueryClient();
  const [value, setValue] = useState("");
  const refresh = () => void client.invalidateQueries({ queryKey: ["provider-keys", workspaceId] });
  const save = useMutation({
    mutationFn: () => api.saveProviderKey(workspaceId, provider, value.trim()),
    onSuccess: () => {
      setValue("");
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: () => api.removeProviderKey(workspaceId, provider), onSuccess: refresh });
  const failure = save.error ?? remove.error;
  return (
    <li className="py-4 first:pt-0 last:pb-0">
      <h3 className="text-[15px] font-medium">{name}</h3>
      {current ? (
        <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
          <KeyRound aria-hidden="true" className="size-4 text-muted" />
          <span className="font-mono text-[13px]">{current.hint}</span>
          <span className="text-muted">
            Added {new Date(current.created_at).toLocaleDateString()}
            {current.created_by_name ? ` by ${current.created_by_name}` : ""}
          </span>
          {owner ? (
            <Button
              variant="destructive"
              size="sm"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
              aria-label={`Remove key for ${name}`}
            >
              Remove key
            </Button>
          ) : null}
        </p>
      ) : !owner ? (
        <p className="mt-1 text-muted">No key. Workspace owners can add one.</p>
      ) : null}
      {owner ? (
        <form
          className="mt-3 grid max-w-2xl gap-4 sm:grid-cols-[1fr_auto] sm:items-start"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <Field label={current ? `Replace with a new ${name} API key` : `${name} API key`}>
            <Input type="password" autoComplete="off" spellCheck={false} value={value} onChange={(e) => setValue(e.target.value)} />
          </Field>
          <div className="flex flex-col gap-1.5">
            <span aria-hidden="true" className="hidden text-[13px] font-medium sm:block">
              &nbsp;
            </span>
            <Button type="submit" disabled={save.isPending || value.trim().length < 20} aria-label={`Check and save the ${name} key`}>
              {save.isPending ? "Checking…" : "Check and save"}
            </Button>
          </div>
        </form>
      ) : null}
      {failure ? (
        <p role="alert" className="mt-3 text-destructive">
          {failure instanceof ApiError ? failure.message : "That did not work. Try again."}
        </p>
      ) : null}
    </li>
  );
}
