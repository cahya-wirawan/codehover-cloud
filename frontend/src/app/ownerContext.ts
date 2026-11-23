import { useQuery } from "@tanstack/react-query";
import { useLocation, useParams, useSearchParams } from "react-router";

import { authKeys, fetchCurrentUser } from "../lib/api/auth";
import { api, type OrgRole, type Workspace } from "../lib/api/resources";

/** Whose workspaces the app shows (0038): the person or one of their organizations, like GitHub's
 * context switcher. */
export interface OwnerOption {
  kind: "user" | "organization";
  handle: string;
  name: string;
  organizationId: string | null;
  role: OrgRole | null; // the person's organization role; null for themselves
}

const STORE = "codehover.owner";

/** The last chosen owner, per browser; storage can be unavailable (private windows), then it is "you". */
export function rememberOwner(handle: string): void {
  try {
    localStorage.setItem(STORE, handle);
  } catch {
    // ignored: the choice is a convenience
  }
}

function remembered(): string | null {
  try {
    return localStorage.getItem(STORE);
  } catch {
    return null;
  }
}

/** The address that shows an owner's workspaces. */
export const ownerHome = (handle: string) => `/w?${new URLSearchParams({ owner: handle }).toString()}`;

/** Whether a workspace belongs to an owner (workspaces from before 0038 have no owner: the person's). */
export function ownedBy(workspace: Workspace, owner: OwnerOption): boolean {
  if (!workspace.owner) return owner.kind === "user";
  return workspace.owner.kind === owner.kind && workspace.owner.handle === owner.handle;
}

/** The current owner: the owner of the workspace or project on screen, the organization of /o/:oid,
 * or on /w the `owner` in the address, else the last choice, else the person. */
export function useOwnerContext(): {
  current: OwnerOption | null;
  options: OwnerOption[];
  invitations: number;
  workspaces: Workspace[];
} {
  const params = useParams();
  const location = useLocation();
  const [search] = useSearchParams();
  const user = useQuery({ queryKey: authKeys.me, queryFn: ({ signal }) => fetchCurrentUser(signal), retry: false });
  const orgs = useQuery({ queryKey: ["organizations"], queryFn: api.organizations, retry: false, enabled: Boolean(user.data) });
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces, retry: false, enabled: Boolean(user.data) });
  const project = useQuery({
    queryKey: ["project", params.pid],
    queryFn: () => api.project(params.pid ?? ""),
    enabled: Boolean(params.pid),
    retry: false,
  });
  const me: OwnerOption | null = user.data?.handle
    ? { kind: "user", handle: user.data.handle, name: user.data.display_name, organizationId: null, role: null }
    : null;
  const options: OwnerOption[] = [
    ...(me ? [me] : []),
    ...(orgs.data?.items ?? []).map((o) => ({
      kind: "organization" as const,
      handle: o.handle,
      name: o.name,
      organizationId: o.id,
      role: o.role,
    })),
  ];
  const items = workspaces.data?.items ?? [];
  const wid = params.wid ?? project.data?.workspace_id ?? null;
  const onScreen = wid ? items.find((w) => w.id === wid) : undefined;
  let wanted: string | null = null;
  if (onScreen) wanted = onScreen.owner?.handle ?? me?.handle ?? null;
  else if (params.oid) wanted = options.find((o) => o.organizationId === params.oid)?.handle ?? null;
  else if (location.pathname === "/w") wanted = search.get("owner") ?? remembered();
  else wanted = remembered();
  const current = options.find((o) => o.handle === wanted) ?? me;
  return { current, options, invitations: orgs.data?.invitations?.length ?? 0, workspaces: items };
}
