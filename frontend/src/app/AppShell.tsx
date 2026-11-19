import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ChevronsUpDown,
  CreditCard,
  FolderGit2,
  GitBranch,
  LayoutGrid,
  Menu,
  ScrollText,
  Users,
  X,
} from "lucide-react";
import { Dialog } from "radix-ui";
import { type ReactNode, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useParams } from "react-router";

import { Button } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { Toaster } from "../components/ui/toaster";
import { AccountMenu } from "../features/auth/AccountMenu";
import { authKeys, fetchCurrentUser } from "../lib/api/auth";
import { api } from "../lib/api/resources";
import { cn } from "../lib/cn";

/** The workspace the current route belongs to: from /w/:wid, or from the project of /p/:pid. */
function useCurrentWorkspace(): { wid: string | null; projectName: string | null; buildId: string | null } {
  const params = useParams();
  const project = useQuery({
    queryKey: ["project", params.pid],
    queryFn: () => api.project(params.pid ?? ""),
    enabled: Boolean(params.pid),
    retry: false,
  });
  return {
    wid: params.wid ?? project.data?.workspace_id ?? null,
    projectName: project.data?.name ?? null,
    buildId: params.bid ?? null,
  };
}

function Wordmark() {
  return (
    <Link to="/" className="flex items-center gap-2 rounded-[var(--radius-control)] font-semibold tracking-tight">
      <span aria-hidden="true" className="grid size-6 place-items-center rounded-[5px] bg-primary font-mono text-[13px] text-primary-foreground">
        {"{"}
      </span>
      <span>
        codehover<span className="font-normal text-muted">-cloud</span>
      </span>
    </Link>
  );
}

function NavItem({ to, icon, children, end }: { to: string; icon: ReactNode; children: ReactNode; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end ?? false}
      className={({ isActive }) =>
        cn(
          "flex h-8 items-center gap-2.5 rounded-[var(--radius-control)] px-2.5 text-[13px] text-muted transition-colors duration-150 hover:bg-surface-muted hover:text-foreground [&_svg]:size-4",
          isActive && "bg-surface-muted font-medium text-foreground",
        )
      }
    >
      {icon}
      {children}
    </NavLink>
  );
}

function Sidebar({ operator }: { operator: boolean }) {
  const { wid } = useCurrentWorkspace();
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces, retry: false });
  const current = workspaces.data?.items.find((w) => w.id === wid);
  return (
    <div className="flex h-full flex-col gap-6 px-3 py-4">
      <div className="px-1.5">
        <Wordmark />
      </div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="flex h-10 w-full items-center gap-2 rounded-[var(--radius-control)] border border-border bg-surface px-2.5 text-left hover:bg-surface-muted"
          >
            <span className="grid size-6 place-items-center rounded-[5px] bg-surface-muted text-xs font-semibold" aria-hidden="true">
              {(current?.name ?? "W").slice(0, 1).toUpperCase()}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-medium">{current?.name ?? "Choose a workspace"}</span>
              {current ? <span className="block text-xs text-muted capitalize">{current.role}</span> : null}
            </span>
            <ChevronsUpDown aria-hidden="true" className="size-4 text-muted" />
            <span className="sr-only">Switch workspace</span>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-[208px]">
          <DropdownMenuLabel>Workspaces</DropdownMenuLabel>
          {workspaces.data?.items.map((w) => (
            <DropdownMenuItem key={w.id} asChild>
              <Link to={`/w/${w.id}`}>
                <span className="truncate">{w.name}</span>
                <span className="ml-auto text-xs text-muted capitalize">{w.role}</span>
              </Link>
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild>
            <Link to="/w">
              <LayoutGrid aria-hidden="true" />
              All workspaces
            </Link>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <nav aria-label="Workspace" className="flex flex-col gap-0.5">
        {wid ? (
          <>
            <NavItem to={`/w/${wid}`} icon={<FolderGit2 aria-hidden="true" />} end>
              Projects
            </NavItem>
            <NavItem to={`/w/${wid}/members`} icon={<Users aria-hidden="true" />}>
              Members
            </NavItem>
            <NavItem to={`/w/${wid}/connections`} icon={<GitBranch aria-hidden="true" />}>
              GitHub connections
            </NavItem>
            <NavItem to={`/w/${wid}/usage`} icon={<CreditCard aria-hidden="true" />}>
              Plan and usage
            </NavItem>
            <NavItem to={`/w/${wid}/audit`} icon={<ScrollText aria-hidden="true" />}>
              Audit log
            </NavItem>
          </>
        ) : (
          <NavItem to="/w" icon={<LayoutGrid aria-hidden="true" />} end>
            Workspaces
          </NavItem>
        )}
      </nav>
      {operator ? (
        <nav aria-label="Platform" className="mt-auto flex flex-col gap-0.5 border-t border-border pt-4">
          <NavItem to="/ops" icon={<Activity aria-hidden="true" />}>
            Operations
          </NavItem>
        </nav>
      ) : null}
    </div>
  );
}

function Breadcrumbs() {
  const { wid, projectName, buildId } = useCurrentWorkspace();
  const params = useParams();
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces, retry: false, enabled: Boolean(wid) });
  const workspace = workspaces.data?.items.find((w) => w.id === wid);
  const crumbs: { label: string; to?: string }[] = [];
  if (workspace) crumbs.push({ label: workspace.name, to: `/w/${workspace.id}` });
  if (projectName && params.pid) crumbs.push({ label: projectName, to: `/p/${params.pid}` });
  if (buildId) crumbs.push({ label: "Build" });
  if (!crumbs.length) return <span />;
  return (
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="flex min-w-0 items-center gap-1.5 text-[13px] text-muted">
        {crumbs.map((crumb, index) => (
          <li key={`${crumb.label}-${index}`} className="flex min-w-0 items-center gap-1.5">
            {index ? <span aria-hidden="true">/</span> : null}
            {crumb.to && index < crumbs.length - 1 ? (
              <Link to={crumb.to} className="truncate hover:text-foreground">
                {crumb.label}
              </Link>
            ) : (
              <span aria-current="page" className="truncate font-medium text-foreground">
                {crumb.label}
              </span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

const BARE = /^\/(login|sites\/|invitations\/)/;

export function AppShell() {
  const location = useLocation();
  const user = useQuery({ queryKey: authKeys.me, queryFn: ({ signal }) => fetchCurrentUser(signal), retry: false });
  const [drawer, setDrawer] = useState(false);
  const signedIn = Boolean(user.data);
  const withSidebar = signedIn && !BARE.test(location.pathname) && location.pathname !== "/";

  return (
    <div className="min-h-dvh bg-background text-foreground">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:rounded-[var(--radius-control)] focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      {withSidebar ? (
        <aside className="fixed inset-y-0 left-0 hidden w-[232px] border-r border-border bg-surface lg:block" aria-label="Sidebar">
          <Sidebar operator={Boolean(user.data?.operator)} />
        </aside>
      ) : null}
      <div className={cn("flex min-h-dvh flex-col", withSidebar && "lg:pl-[232px]")}>
        <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-border bg-surface/95 px-4 backdrop-blur sm:px-6">
          {withSidebar ? (
            <Dialog.Root open={drawer} onOpenChange={setDrawer}>
              <Dialog.Trigger asChild>
                <Button variant="ghost" size="icon" className="lg:hidden">
                  <Menu aria-hidden="true" />
                  <span className="sr-only">Open navigation</span>
                </Button>
              </Dialog.Trigger>
              <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 z-40 bg-foreground/30 lg:hidden" />
                <Dialog.Content
                  className="fixed inset-y-0 left-0 z-50 w-[264px] border-r border-border bg-surface shadow-[var(--shadow-overlay)] lg:hidden"
                  onClick={(event) => {
                    if ((event.target as HTMLElement).closest("a")) setDrawer(false); // navigated: close
                  }}
                >
                  <Dialog.Title className="sr-only">Navigation</Dialog.Title>
                  <Dialog.Description className="sr-only">Workspaces and pages</Dialog.Description>
                  <Dialog.Close asChild>
                    <Button variant="ghost" size="icon" className="absolute top-3 right-3">
                      <X aria-hidden="true" />
                      <span className="sr-only">Close navigation</span>
                    </Button>
                  </Dialog.Close>
                  <Sidebar operator={Boolean(user.data?.operator)} />
                </Dialog.Content>
              </Dialog.Portal>
            </Dialog.Root>
          ) : (
            <Wordmark />
          )}
          <div className="min-w-0 flex-1">{withSidebar ? <Breadcrumbs /> : null}</div>
          <AccountMenu />
        </header>
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1440px] flex-1 px-4 py-8 outline-none sm:px-8">
          <Outlet />
        </main>
        {location.pathname.startsWith("/sites/") ? null : (
          <footer className="mx-auto flex w-full max-w-[1440px] flex-wrap items-center justify-center gap-x-5 gap-y-2 border-t border-border px-4 py-5 text-[13px] text-muted sm:px-8">
            <nav aria-label="About this service" className="flex flex-wrap justify-center gap-x-5 gap-y-2">
              <Link to="/privacy" className="hover:text-foreground">
                Privacy
              </Link>
              <Link to="/terms" className="hover:text-foreground">
                Terms
              </Link>
              <Link to="/support" className="hover:text-foreground">
                Support
              </Link>
            </nav>
          </footer>
        )}
      </div>
      <Toaster />
    </div>
  );
}
