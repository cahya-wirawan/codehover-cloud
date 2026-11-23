import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { ownedBy, ownerHome, rememberOwner, useOwnerContext } from "../../app/ownerContext";
import { Button } from "../../components/ui/button";
import { Field, Input } from "../../components/ui/input";
import { EmptyState, PageHeader, Section } from "../../components/ui/section";
import { Skeleton } from "../../components/ui/skeleton";
import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";
import { HANDLE_HINT, HANDLE_PATTERN } from "../account/WelcomePage";

/** The workspaces of the current owner (the context switcher in the sidebar, 0038): the person's own,
 * or an organization's that the person may see. Open invitations show on the person's own page. */
export function WorkspacesPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { current: owner, workspaces } = useOwnerContext();
  const all = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const orgs = useQuery({ queryKey: ["organizations"], queryFn: api.organizations });
  const [name, setName] = useState("");
  useEffect(() => {
    if (owner) rememberOwner(owner.handle);
  }, [owner]);
  const isOrg = owner?.kind === "organization";
  const mayCreate = !isOrg || owner.role === "owner" || owner.role === "admin";
  const create = useMutation({
    mutationFn: () => api.createWorkspace(name.trim(), isOrg ? owner.organizationId : null),
    onSuccess: async () => {
      setName("");
      await queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
  const answer = useMutation({
    mutationFn: ({ id, accept }: { id: string; accept: boolean }) => api.answerOrgInvitation(id, accept),
    onSuccess: async (result, { accept }) => {
      // Accepting switches to the organization joined.
      const joined = orgs.data?.invitations?.find((i) => i.organization.id === result.organization_id);
      await queryClient.invalidateQueries({ queryKey: ["organizations"] });
      await queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      if (accept && joined) {
        rememberOwner(joined.organization.handle);
        void navigate(ownerHome(joined.organization.handle));
      }
    },
  });
  const items = owner ? workspaces.filter((w) => ownedBy(w, owner)) : workspaces;

  return (
    <section aria-labelledby="ws-title" className="max-w-3xl">
      <PageHeader
        title={<span id="ws-title">{isOrg ? owner.name : "Your workspaces"}</span>}
        description={
          isOrg
            ? `Workspaces of the organization ${owner.handle} that you can open. Its owners and admins manage all of them.`
            : "A workspace holds projects and the people who can read them. Everyone in a workspace sees its projects, unless an admin restricts a project to chosen members."
        }
        actions={
          isOrg && owner.organizationId ? (
            <Button asChild variant="secondary">
              <Link to={`/o/${owner.organizationId}`}>Organization settings</Link>
            </Button>
          ) : null
        }
      />
      {!isOrg && orgs.data?.invitations?.length ? (
        <Section title="Invitations" className="mb-8">
          <ul className="divide-y divide-border">
            {orgs.data.invitations.map((i) => (
              <li key={i.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3">
                <span>
                  Join <span className="font-medium">{i.organization.name}</span> as {i.role}
                </span>
                <Button size="sm" variant="primary" onClick={() => answer.mutate({ id: i.id, accept: true })} disabled={answer.isPending}>
                  Accept
                </Button>
                <Button size="sm" variant="secondary" onClick={() => answer.mutate({ id: i.id, accept: false })} disabled={answer.isPending}>
                  Decline
                </Button>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      {all.isPending ? (
        <div className="flex flex-col gap-2" aria-hidden="true">
          <Skeleton className="h-14" />
          <Skeleton className="h-14" />
        </div>
      ) : null}
      {all.data && items.length === 0 ? (
        <EmptyState title="No workspaces yet">
          {mayCreate
            ? "Create a workspace below, then connect a repository to it."
            : "You have not been added to any of this organization's workspaces yet."}
        </EmptyState>
      ) : null}
      {items.length ? (
        <ul className="panel divide-y divide-border">
          {items.map((w) => (
            <li key={w.id}>
              <Link to={`/w/${w.id}`} className="group flex items-center gap-3 px-4 py-3 hover:bg-surface-muted/60">
                <span aria-hidden="true" className="grid size-8 place-items-center rounded-[6px] bg-surface-muted font-semibold">
                  {w.name.slice(0, 1).toUpperCase()}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{w.name}</span>
                </span>
                <span className="text-xs text-muted capitalize">{w.role}</span>
                <ChevronRight aria-hidden="true" className="size-4 text-muted group-hover:text-foreground" />
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
      {mayCreate ? (
        <Section title={isOrg ? `New workspace in ${owner.name}` : "New workspace"} className="mt-10">
          <form
            className="flex max-w-xl flex-col gap-4 sm:flex-row sm:items-end"
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate();
            }}
          >
            <Field label="Name" className="flex-1">
              <Input required minLength={2} maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Button type="submit" variant="primary" disabled={create.isPending}>
              Create workspace
            </Button>
          </form>
          {create.error ? (
            <p role="alert" className="mt-3 text-destructive">
              {create.error instanceof ApiError ? create.error.message : "Could not create the workspace."}
            </p>
          ) : null}
        </Section>
      ) : (
        <p className="mt-8 text-muted">Only the organization's owners and admins create its workspaces.</p>
      )}
    </section>
  );
}

/** A new organization (0038); its creator is its owner. The app then switches to it. */
export function NewOrganizationPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [handle, setHandle] = useState("");
  const create = useMutation({
    mutationFn: () => api.createOrganization(name.trim(), handle.trim()),
    onSuccess: async (org) => {
      await queryClient.invalidateQueries({ queryKey: ["organizations"] });
      rememberOwner(org.handle);
      void navigate(ownerHome(org.handle));
    },
  });
  return (
    <section aria-labelledby="org-new-title" className="max-w-xl">
      <PageHeader
        title={<span id="org-new-title">New organization</span>}
        description="An organization owns workspaces together, with one plan. Its owners and admins manage all its workspaces; members see the ones they are added to."
      />
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Organization name">
          <Input required minLength={2} maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Name in addresses" hint={HANDLE_HINT}>
          <Input
            required
            maxLength={39}
            pattern={HANDLE_PATTERN}
            value={handle}
            onChange={(e) => setHandle(e.target.value.toLowerCase())}
            placeholder="acme"
          />
        </Field>
        <div>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            Create organization
          </Button>
        </div>
      </form>
      {create.error ? (
        <p role="alert" className="mt-3 text-destructive">
          {create.error instanceof ApiError ? create.error.message : "Could not create the organization."}
        </p>
      ) : null}
    </section>
  );
}
