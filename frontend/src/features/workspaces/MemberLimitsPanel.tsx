import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api, formatUsd, type MemberUsage, type Role } from "../../lib/api/resources";

const MANAGEABLE_BY_ADMIN: Role[] = ["viewer", "editor"];

/** Dollars typed by a person to integer micro-USD; null when it is not an amount. */
function toMicro(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d{1,7}(\.\d{1,2})?$/.test(trimmed)) return null;
  const [whole, cents = ""] = trimmed.split(".");
  return Number(whole) * 1_000_000 + Number(cents.padEnd(2, "0")) * 10_000;
}

function LimitCell({ wid, member, editable }: { wid: string; member: MemberUsage; editable: boolean }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const save = useMutation({
    mutationFn: (limit: number | null) => api.setSpendingLimit(wid, member.user_id, limit),
    onSuccess: async () => {
      setEditing(false);
      await queryClient.invalidateQueries({ queryKey: ["usage-members", wid] });
      await queryClient.invalidateQueries({ queryKey: ["usage", wid] });
    },
  });
  const name = member.handle ?? member.display_name;
  const current = member.limit_micro_usd === null ? "No limit" : `${formatUsd(member.limit_micro_usd)} a month`;
  if (!editing) {
    return (
      <div className="flex items-center justify-end gap-3">
        <span className={member.limit_micro_usd === null ? "text-muted" : "font-mono text-[13px]"}>{current}</span>
        {editable ? (
          <Button
            size="sm"
            variant="ghost"
            aria-label={`Change the spending limit of ${name}`}
            onClick={() => {
              setValue(member.limit_micro_usd === null ? "" : (member.limit_micro_usd / 1_000_000).toFixed(2));
              setEditing(true);
            }}
          >
            Change
          </Button>
        ) : null}
      </div>
    );
  }
  const amount = toMicro(value);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (amount !== null) save.mutate(amount);
  };
  return (
    <form onSubmit={submit} className="flex flex-col items-end gap-1.5">
      <div className="flex items-center gap-2">
        <label className="flex items-center gap-1.5">
          <span aria-hidden="true" className="text-muted">$</span>
          <span className="sr-only">Monthly limit of {name} in US dollars</span>
          <Input
            inputMode="decimal"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="h-8 w-24 font-mono text-[13px]"
            aria-invalid={value !== "" && amount === null}
            autoFocus
          />
        </label>
        <Button size="sm" variant="primary" type="submit" disabled={amount === null || save.isPending}>
          Save
        </Button>
        {member.limit_micro_usd !== null ? (
          <Button size="sm" variant="secondary" type="button" onClick={() => save.mutate(null)} disabled={save.isPending}>
            Remove limit
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" type="button" onClick={() => setEditing(false)}>
          Cancel
        </Button>
      </div>
      {save.error ? (
        <p role="alert" className="text-xs text-destructive">
          {save.error instanceof ApiError ? save.error.message : "Could not change the limit."}
        </p>
      ) : null}
    </form>
  );
}

/** Spending by member this month, and each member's monthly limit (0044): owners and admins only. Owners
 * limit anyone; admins limit editors and viewers. A limit counts the member's own builds on the plan
 * allowance; builds on the workspace's own key are billed by the provider and are not counted. */
export function MemberLimitsPanel({ wid, role }: { wid: string; role: Role }) {
  const members = useQuery({ queryKey: ["usage-members", wid], queryFn: () => api.usageByMember(wid) });
  return (
    <Section
      title="By member"
      description="What each member's builds used this month on the plan allowance. A monthly limit stops a member from starting builds beyond it; others are not held back."
    >
      {members.data ? (
        <div className="panel overflow-x-auto">
          <table className="w-full text-left">
            <caption className="sr-only">Spending and limits by member</caption>
            <thead>
              <tr className="border-b border-border text-xs text-muted">
                <th scope="col" className="px-4 py-2.5 font-medium">Member</th>
                <th scope="col" className="px-4 py-2.5 font-medium">Role</th>
                <th scope="col" className="px-4 py-2.5 text-right font-medium">Builds</th>
                <th scope="col" className="px-4 py-2.5 text-right font-medium">Used</th>
                <th scope="col" className="px-4 py-2.5 text-right font-medium">Monthly limit</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {members.data.items.map((m) => (
                <tr key={m.user_id}>
                  <th scope="row" className="px-4 py-2.5 font-medium">
                    {m.display_name}
                    {m.handle ? <span className="block text-xs font-normal text-muted">{m.handle}</span> : null}
                  </th>
                  <td className="px-4 py-2.5 capitalize">{m.role}</td>
                  <td className="px-4 py-2.5 text-right font-mono text-[13px]">{m.builds}</td>
                  <td className="px-4 py-2.5 text-right font-mono text-[13px]">
                    {formatUsd(m.settled_micro_usd)}
                    {m.reserved_micro_usd ? <span className="block text-xs text-muted">{formatUsd(m.reserved_micro_usd)} held</span> : null}
                  </td>
                  <td className="px-4 py-2.5">
                    <LimitCell wid={wid} member={m} editable={role === "owner" || (role === "admin" && MANAGEABLE_BY_ADMIN.includes(m.role))} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : members.error ? (
        <p role="alert" className="text-destructive">Could not load spending by member.</p>
      ) : (
        <p className="text-muted">Loading members…</p>
      )}
    </Section>
  );
}
