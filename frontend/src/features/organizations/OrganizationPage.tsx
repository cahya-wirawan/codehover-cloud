import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { Button } from "../../components/ui/button";
import { Field, Input, Select } from "../../components/ui/input";
import { EmptyState, PageHeader, Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api, type OrgRole } from "../../lib/api/resources";
import { HANDLE_HINT, HANDLE_PATTERN } from "../account/WelcomePage";

const ROLES: OrgRole[] = ["member", "admin", "owner"];

function failure(error: unknown, fallback: string): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.message : fallback;
}

/** An organization (0038): its workspaces, members, invitations and settings. Owners and admins of the
 * organization are owners and admins of all its workspaces. */
export function OrganizationPage() {
  const { oid = "" } = useParams();
  const client = useQueryClient();
  const navigate = useNavigate();
  const org = useQuery({ queryKey: ["organization", oid], queryFn: () => api.organization(oid) });
  const role = org.data?.role;
  const manager = role === "owner" || role === "admin";
  const workspaces = useQuery({ queryKey: ["org-workspaces", oid], queryFn: () => api.organizationWorkspaces(oid) });
  const members = useQuery({ queryKey: ["org-members", oid], queryFn: () => api.organizationMembers(oid) });
  const invitations = useQuery({
    queryKey: ["org-invitations", oid],
    queryFn: () => api.organizationInvitations(oid),
    enabled: manager,
  });
  const refresh = (...keys: string[]) => {
    for (const key of keys) void client.invalidateQueries({ queryKey: [key, oid] });
  };
  const [inviteHandle, setInviteHandle] = useState("");
  const [inviteRole, setInviteRole] = useState<OrgRole>("member");
  const invite = useMutation({
    mutationFn: () => api.inviteToOrganization(oid, inviteHandle.trim().toLowerCase(), inviteRole),
    onSuccess: () => {
      setInviteHandle("");
      refresh("org-invitations");
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeOrgInvitation(oid, id),
    onSuccess: () => refresh("org-invitations"),
  });
  const changeRole = useMutation({
    mutationFn: ({ userId, next }: { userId: string; next: OrgRole }) => api.changeOrgRole(oid, userId, next),
    onSuccess: () => refresh("org-members", "org-workspaces"),
  });
  const remove = useMutation({
    mutationFn: (userId: string) => api.removeOrgMember(oid, userId),
    onSuccess: () => refresh("org-members"),
  });
  const [handle, setHandle] = useState<string | null>(null);
  const rename = useMutation({
    mutationFn: () => api.setOrganizationHandle(oid, (handle ?? "").trim()),
    onSuccess: () => {
      setHandle(null);
      refresh("organization");
    },
  });
  const destroy = useMutation({
    mutationFn: () => api.deleteOrganization(oid),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["organizations"] });
      void navigate("/w");
    },
  });

  if (org.isError) {
    return (
      <EmptyState title="Organization not found">
        It may have been deleted, or you are not a member. <Link to="/w">Back to workspaces</Link>
      </EmptyState>
    );
  }
  if (!org.data) return <p role="status" className="text-muted">Loading…</p>;
  return (
    <section aria-labelledby="org-title" className="max-w-3xl">
      <PageHeader
        title={<span id="org-title">{org.data.name}</span>}
        description={
          <>
            Address name <span className="font-mono">{org.data.handle}</span>; your role is <span className="capitalize">{role}</span>.
            Owners and admins manage every workspace of the organization; members see the workspaces they are added to.
          </>
        }
      />

      <Section title="Workspaces">
        {workspaces.data?.items.length ? (
          <ul className="panel divide-y divide-border">
            {workspaces.data.items.map((w) => (
              <li key={w.id}>
                <Link to={`/w/${w.id}`} className="group flex items-center gap-3 px-4 py-3 hover:bg-surface-muted/60">
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
        ) : (
          <p className="text-muted">
            {manager
              ? "No workspaces yet. Create one on the workspaces page and choose this organization as its owner."
              : "You have not been added to any of this organization's workspaces."}
          </p>
        )}
      </Section>

      <Section title="Members" className="mt-10">
        <ul className="divide-y divide-border">
          {members.data?.items.map((m) => (
            <li key={m.user_id} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3">
              <span className="font-medium">{m.display_name}</span>
              <span className="font-mono text-[13px] text-muted">{m.handle}</span>
              {role === "owner" ? (
                <Select
                  aria-label={`Role of ${m.handle}`}
                  className="w-auto"
                  value={m.role}
                  disabled={changeRole.isPending}
                  onChange={(e) => changeRole.mutate({ userId: m.user_id, next: e.target.value as OrgRole })}
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </Select>
              ) : (
                <span className="text-xs text-muted capitalize">{m.role}</span>
              )}
              {role === "owner" || (role === "admin" && m.role === "member") ? (
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => remove.mutate(m.user_id)}
                  disabled={remove.isPending}
                  aria-label={`Remove ${m.handle}`}
                >
                  Remove
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
        {failure(changeRole.error ?? remove.error, "That did not work. Try again.") ? (
          <p role="alert" className="mt-3 text-destructive">
            {failure(changeRole.error ?? remove.error, "That did not work. Try again.")}
          </p>
        ) : null}
      </Section>

      {manager ? (
        <Section title="Invite someone" className="mt-10" description="People accept an invitation on their workspaces page.">
          <form
            className="grid gap-4 sm:grid-cols-[1fr_auto_auto] sm:items-end"
            onSubmit={(event) => {
              event.preventDefault();
              invite.mutate();
            }}
          >
            <Field label="Their name">
              <Input required maxLength={39} value={inviteHandle} onChange={(e) => setInviteHandle(e.target.value)} />
            </Field>
            <Field label="Role">
              <Select value={inviteRole} onChange={(e) => setInviteRole(e.target.value as OrgRole)}>
                {(role === "owner" ? ROLES : (["member"] as OrgRole[])).map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </Select>
            </Field>
            <Button type="submit" disabled={invite.isPending || !inviteHandle.trim()}>
              Invite
            </Button>
          </form>
          {failure(invite.error, "Could not invite.") ? (
            <p role="alert" className="mt-3 text-destructive">
              {failure(invite.error, "Could not invite.")}
            </p>
          ) : null}
          {invitations.data?.items.length ? (
            <ul className="mt-4 divide-y divide-border">
              {invitations.data.items.map((i) => (
                <li key={i.id} className="flex flex-wrap items-center gap-x-3 py-2">
                  <span className="font-mono text-[13px]">{i.handle}</span>
                  <span className="text-xs text-muted capitalize">{i.role}</span>
                  <span className="text-xs text-muted">expires {new Date(i.expires_at).toLocaleDateString()}</span>
                  <Button size="sm" variant="secondary" onClick={() => revoke.mutate(i.id)} aria-label={`Revoke the invitation of ${i.handle}`}>
                    Revoke
                  </Button>
                </li>
              ))}
            </ul>
          ) : null}
        </Section>
      ) : null}

      {role === "owner" ? (
        <Section title="Settings" className="mt-10">
          <form
            className="flex max-w-xl flex-col gap-4 sm:flex-row sm:items-end"
            onSubmit={(event) => {
              event.preventDefault();
              rename.mutate();
            }}
          >
            <Field label="Organization name in addresses" hint={HANDLE_HINT} className="flex-1">
              <Input
                required
                maxLength={39}
                pattern={HANDLE_PATTERN}
                value={handle ?? org.data.handle}
                onChange={(e) => setHandle(e.target.value.toLowerCase())}
              />
            </Field>
            <Button type="submit" disabled={rename.isPending || handle === null || handle === org.data.handle}>
              Rename
            </Button>
          </form>
          {failure(rename.error, "Could not rename.") ? (
            <p role="alert" className="mt-3 text-destructive">
              {failure(rename.error, "Could not rename.")}
            </p>
          ) : null}
          <div className="mt-6">
            <Button variant="destructive" onClick={() => destroy.mutate()} disabled={destroy.isPending}>
              Delete organization
            </Button>
            <p className="mt-2 text-xs text-muted">Only an organization without workspaces can be deleted. Its name stays reserved for 90 days.</p>
            {failure(destroy.error, "Could not delete the organization.") ? (
              <p role="alert" className="mt-2 text-destructive">
                {failure(destroy.error, "Could not delete the organization.")}
              </p>
            ) : null}
          </div>
        </Section>
      ) : null}
    </section>
  );
}
