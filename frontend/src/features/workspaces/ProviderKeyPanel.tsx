import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { Field, Input } from "../../components/ui/input";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

/** A workspace's own Anthropic key (M6, T24): stored sealed, shown only as a hint, used for builds that
 * choose it. Owners manage it; the key is checked with the provider before it is saved. */
export function ProviderKeyPanel({ workspaceId, owner }: { workspaceId: string; owner: boolean }) {
  const client = useQueryClient();
  const keys = useQuery({ queryKey: ["provider-keys", workspaceId], queryFn: () => api.providerKeys(workspaceId) });
  const [value, setValue] = useState("");
  const refresh = () => void client.invalidateQueries({ queryKey: ["provider-keys", workspaceId] });
  const save = useMutation({
    mutationFn: () => api.saveProviderKey(workspaceId, value.trim()),
    onSuccess: () => {
      setValue("");
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: () => api.removeProviderKey(workspaceId), onSuccess: refresh });
  const current = keys.data?.items.find((k) => k.provider === "anthropic");
  const failure = save.error ?? remove.error;
  return (
    <Section
      title="Your own model provider key"
      description="Builds that choose it are billed by Anthropic to your account and do not use the plan allowance. The key is stored encrypted and never shown again."
    >
      {current ? (
        <p className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <KeyRound aria-hidden="true" className="size-4 text-muted" />
          <span className="font-mono text-[13px]">{current.hint}</span>
          <span className="text-muted">
            Added {new Date(current.created_at).toLocaleDateString()}
            {current.created_by_name ? ` by ${current.created_by_name}` : ""}
          </span>
          {owner ? (
            <Button variant="destructive" size="sm" onClick={() => remove.mutate()} disabled={remove.isPending}>
              Remove key
            </Button>
          ) : null}
        </p>
      ) : !owner ? (
        <p className="text-muted">No key. Workspace owners can add one.</p>
      ) : null}
      {owner ? (
        <form
          className="mt-4 grid max-w-2xl gap-4 sm:grid-cols-[1fr_auto] sm:items-start"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <Field label={current ? "Replace with a new Anthropic API key" : "Anthropic API key"}>
            <Input type="password" autoComplete="off" spellCheck={false} value={value} onChange={(e) => setValue(e.target.value)} />
          </Field>
          <div className="flex flex-col gap-1.5">
            <span aria-hidden="true" className="hidden text-[13px] font-medium sm:block">
              &nbsp;
            </span>
            <Button type="submit" disabled={save.isPending || value.trim().length < 20}>
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
    </Section>
  );
}
