import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, UserPlus } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";

import { Button } from "../../components/ui/button";
import { Field, Input, Select } from "../../components/ui/input";
import { PageHeader, Section } from "../../components/ui/section";
import { Skeleton } from "../../components/ui/skeleton";
import { authKeys, fetchCurrentUser } from "../../lib/api/auth";
import { ApiError } from "../../lib/api/client";
import { api, type Role } from "../../lib/api/resources";

const ROLE_HELP: Record<Role, string> = {
  viewer: "Reads projects and sites",
  editor: "Also starts builds",
  admin: "Also manages members, access and publishing",
  owner: "Also manages owners, billing and deletion",
};

/** S17 Members: who is in the workspace and their roles, invitations, and how project access works.
 * Admins change viewers and editors; owners change anyone. The server enforces both (09). */
export function MembersPage() {
  const { wid = "" } = useParams();
  const client = useQueryClient();
  const navigate = useNavigate();
  const me = useQuery({ queryKey: authKeys.me, queryFn: ({ signal }) => fetchCurrentUser(signal), retry: false });
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const myRole = workspaces.data?.items.find((w) => w.id === wid)?.role;
  const manages = myRole === "admin" || myRole === "owner";
  const members = useQuery({ queryKey: ["members", wid], queryFn: () => api.members(wid) });
  const invitations = useQuery({
    queryKey: ["invitations", wid],
    queryFn: () => api.invitations(wid),
    enabled: manages,
  });
  // The roles this caller may hand out: admins manage viewers and editors only.
  const assignable: Role[] = myRole === "owner" ? ["viewer", "editor", "admin", "owner"] : ["viewer", "editor"];
  const canChange = (role: Role) => myRole === "owner" || (myRole === "admin" && (role === "viewer" || role === "editor"));

  const [email, setEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<Role>("viewer");
  const [link, setLink] = useState<{ email: string; url: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["members", wid] });
    void client.invalidateQueries({ queryKey: ["invitations", wid] });
  };
  const change = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: Role }) => api.changeRole(wid, userId, role),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (userId: string) => api.removeMember(wid, userId),
    onSuccess: async (_, userId) => {
      if (userId === me.data?.id) {
        client.clear();
        await navigate("/w", { replace: true });
        return;
      }
      refresh();
    },
  });
  const invite = useMutation({
    mutationFn: () => api.invite(wid, email.trim(), inviteRole),
    onSuccess: (created) => {
      setLink({ email: created.email, url: created.accept_url });
      setCopied(false);
      setEmail("");
      refresh();
    },
  });
  const revoke = useMutation({ mutationFn: (id: string) => api.revokeInvitation(wid, id), onSuccess: refresh });
  const failure = change.error ?? remove.error ?? invite.error ?? revoke.error;

  return (
    <section aria-labelledby="members-title" className="max-w-5xl">
      <PageHeader
        title={<span id="members-title">Members</span>}
        description="Everyone in the workspace can open its projects, unless an admin restricts a project to chosen members on the project page."
      />
      {members.isPending ? (
        <div className="flex flex-col gap-2" aria-hidden="true">
          <Skeleton className="h-12" />
          <Skeleton className="h-12" />
        </div>
      ) : (
        <div className="panel overflow-x-auto">
          <table className="w-full text-left">
            <caption className="sr-only">Members</caption>
            <thead>
              <tr className="border-b border-border text-xs text-muted">
                <th scope="col" className="px-4 py-2.5 font-medium">Name</th>
                <th scope="col" className="px-4 py-2.5 font-medium">Role</th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {members.data?.items.map((m) => {
                const self = m.user_id === me.data?.id;
                return (
                  <tr key={m.user_id}>
                    <td className="px-4 py-3">
                      <span className="font-medium">{m.display_name}</span>
                      {self ? <span className="ml-2 text-xs text-muted">you</span> : null}
                    </td>
                    <td className="px-4 py-3">
                      {canChange(m.role) && !self ? (
                        <Select
                          aria-label={`Role of ${m.display_name}`}
                          value={m.role}
                          onChange={(e) => change.mutate({ userId: m.user_id, role: e.target.value as Role })}
                          disabled={change.isPending}
                          className="w-36"
                        >
                          {assignable.map((role) => (
                            <option key={role} value={role} className="capitalize">
                              {role}
                            </option>
                          ))}
                        </Select>
                      ) : (
                        <span className="capitalize">{m.role}</span>
                      )}
                      <span className="ml-3 text-[13px] text-muted">{ROLE_HELP[m.role]}</span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {self ? (
                        <Button variant="ghost" size="sm" onClick={() => remove.mutate(m.user_id)} disabled={remove.isPending}>
                          Leave
                        </Button>
                      ) : canChange(m.role) ? (
                        <Button variant="ghost" size="sm" onClick={() => remove.mutate(m.user_id)} disabled={remove.isPending}>
                          Remove
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {failure ? (
        <p role="alert" className="mt-3 text-destructive">
          {failure instanceof ApiError ? failure.message : "Could not change the members."}
        </p>
      ) : null}

      {manages ? (
        <Section
          title="Invite someone"
          description="An invitation is a link for one email address. The person opens it and signs in with an account whose verified email matches; it expires after 7 days."
          className="mt-10"
        >
          <form
            className="grid max-w-2xl gap-4 sm:grid-cols-[1fr_10rem_auto] sm:items-start"
            onSubmit={(event) => {
              event.preventDefault();
              invite.mutate();
            }}
          >
            <Field label="Email">
              <Input type="email" required maxLength={254} value={email} onChange={(e) => setEmail(e.target.value)} />
            </Field>
            <Field label="Role">
              <Select value={inviteRole} onChange={(e) => setInviteRole(e.target.value as Role)}>
                {assignable
                  .filter((role) => role !== "owner")
                  .map((role) => (
                    <option key={role} value={role} className="capitalize">
                      {role}
                    </option>
                  ))}
              </Select>
            </Field>
            <div className="flex flex-col gap-1.5">
              <span aria-hidden="true" className="hidden text-[13px] font-medium sm:block">
                &nbsp;
              </span>
              <Button type="submit" variant="primary" disabled={invite.isPending}>
                <UserPlus aria-hidden="true" /> Create link
              </Button>
            </div>
          </form>
          {link ? (
            <div role="status" className="panel mt-4 max-w-2xl p-4">
              <p>
                Send this link to <span className="font-medium">{link.email}</span>. It is shown only once.
              </p>
              <div className="mt-2 flex gap-2">
                <Input readOnly value={link.url} aria-label="Invitation link" onFocus={(e) => e.target.select()} className="font-mono text-[13px]" />
                <Button
                  onClick={() => {
                    void navigator.clipboard?.writeText(link.url).then(() => setCopied(true));
                  }}
                >
                  {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                  {copied ? "Copied" : "Copy"}
                </Button>
              </div>
            </div>
          ) : null}
          {invitations.data?.items.length ? (
            <>
              <h3 className="mt-6 text-[13px] font-medium">Waiting to be accepted</h3>
              <ul className="panel mt-2 max-w-2xl divide-y divide-border">
                {invitations.data.items.map((i) => (
                  <li key={i.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
                    <span className="font-medium">{i.email}</span>
                    <span className="text-[13px] text-muted capitalize">{i.role}</span>
                    <span className="text-[13px] text-muted">until {new Date(i.expires_at).toLocaleDateString()}</span>
                    <Button variant="ghost" size="sm" className="ml-auto" onClick={() => revoke.mutate(i.id)} disabled={revoke.isPending}>
                      Revoke
                    </Button>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </Section>
      ) : null}
    </section>
  );
}
