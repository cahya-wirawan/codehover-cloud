import { useMutation, useQuery } from "@tanstack/react-query";
import { useParams, useSearchParams } from "react-router";
import { GitBranch, Plus } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { EmptyState, PageHeader } from "../../components/ui/section";

import { api } from "../../lib/api/resources";

// Error codes from /integrations/github/callback (06).
const ERRORS: Record<string, string> = {
  expired: "That GitHub connection attempt expired or was started elsewhere. Please try again.",
  github_identity_required: "Link your GitHub account to your profile first, then connect again.",
  identity_mismatch: "You authorized GitHub with a different account than the one linked to your profile.",
  installation_not_accessible: "Your GitHub account cannot access that installation.",
  forbidden: "Only workspace admins can connect GitHub.",
  denied: "GitHub authorization was cancelled.",
  provider: "GitHub could not confirm the connection. Please try again.",
};

export function ConnectionsPage() {
  const { wid = "" } = useParams();
  const [params] = useSearchParams();
  const error = params.get("error");
  const connections = useQuery({ queryKey: ["connections", wid], queryFn: () => api.connections(wid) });
  const install = useMutation({
    mutationFn: () => api.startInstall(wid),
    onSuccess: ({ install_url }) => window.location.assign(install_url),
  });

  return (
    <section aria-labelledby="c-title" className="max-w-3xl">
      <PageHeader
        title={<span id="c-title">GitHub connections</span>}
        description="A connection grants this workspace read access only to the repositories you choose on GitHub."
        actions={
          <Button variant="primary" onClick={() => install.mutate()} disabled={install.isPending}>
            <Plus aria-hidden="true" /> Connect GitHub
          </Button>
        }
      />
      {params.get("connected") ? (
        <p role="status" className="mb-4 text-success">
          GitHub connected.
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mb-4 text-destructive">
          {ERRORS[error] ?? ERRORS.provider}
        </p>
      ) : null}
      {connections.data?.items.length ? (
        <ul className="panel divide-y divide-border">
          {connections.data.items.map((c) => (
            <li key={c.id} className="flex items-center gap-3 px-4 py-3">
              <GitBranch aria-hidden="true" className="size-4 text-muted" />
              <span className="flex-1 font-medium">{c.account}</span>
              <Badge tone={c.status === "active" ? "success" : "warning"}>{c.status}</Badge>
            </li>
          ))}
        </ul>
      ) : connections.data ? (
        <EmptyState title="No connections yet">Connect GitHub to choose the repositories this workspace may read.</EmptyState>
      ) : null}
    </section>
  );
}
