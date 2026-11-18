import { useMutation, useQuery } from "@tanstack/react-query";
import { useParams, useSearchParams } from "react-router";

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
    <section aria-labelledby="c-title" className="max-w-2xl">
      <h1 id="c-title" className="text-2xl font-medium">GitHub connections</h1>
      <p className="mt-2 text-muted">
        A connection grants this workspace read access only to repositories you can read on GitHub.
      </p>
      {params.get("connected") ? <p role="status" className="mt-4 text-success">GitHub connected.</p> : null}
      {error ? <p role="alert" className="mt-4 text-destructive">{ERRORS[error] ?? ERRORS.provider}</p> : null}
      <ul className="mt-4 divide-y divide-border rounded-lg border border-border bg-surface">
        {connections.data?.items.map((c) => (
          <li key={c.id} className="flex items-center justify-between p-3">
            <span className="font-medium">{c.account}</span>
            <span className="text-muted">{c.status}</span>
          </li>
        ))}
      </ul>
      <button type="button" onClick={() => install.mutate()} disabled={install.isPending}
        className="mt-4 h-10 rounded-lg bg-primary px-4 font-medium text-primary-foreground">
        Connect GitHub
      </button>
    </section>
  );
}
