import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { ConfirmDeletion } from "../../components/ConfirmDeletion";
import { authKeys, fetchCurrentUser } from "../../lib/api/auth";
import { ApiError, resetCsrfToken } from "../../lib/api/client";
import { ACCOUNT_CONFIRMATION, api } from "../../lib/api/resources";

interface SoleWorkspace {
  id: string;
  slug: string;
  name: string;
}

function soleWorkspaces(error: unknown): SoleWorkspace[] {
  if (!(error instanceof ApiError) || error.code !== "OWNERSHIP_TRANSFER_REQUIRED") return [];
  const list = error.details["workspaces"];
  return Array.isArray(list) ? (list as SoleWorkspace[]) : [];
}

/** S18 account: download my data and delete my account (03 "Export and deletion", 09 reauthentication). */
export function AccountPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const user = useQuery({ queryKey: authKeys.me, queryFn: ({ signal }) => fetchCurrentUser(signal) });
  const [sole, setSole] = useState<SoleWorkspace[]>([]);
  const [chosen, setChosen] = useState<Record<string, boolean>>({});

  const download = useMutation({
    mutationFn: api.exportAccount,
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "codehover-account.json";
      link.click();
      URL.revokeObjectURL(url);
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAccount(ACCOUNT_CONFIRMATION, sole.filter((w) => chosen[w.id]).map((w) => w.id)),
    onSuccess: async () => {
      queryClient.clear(); // nothing of the deleted account stays in memory (10)
      resetCsrfToken();
      await navigate("/login", { replace: true });
    },
    onError: (error) => {
      const listed = soleWorkspaces(error);
      if (listed.length) setSole(listed);
    },
  });
  const needsSignIn = [download.error, remove.error].some((e) => e instanceof ApiError && e.code === "REAUTH_REQUIRED");
  const unresolved = sole.some((w) => !chosen[w.id]);

  return (
    <section aria-labelledby="account-title" className="max-w-3xl">
      <h1 id="account-title" className="text-2xl font-medium">Account</h1>
      {user.data ? (
        <p className="mt-1 text-muted">
          Signed in as {user.data.display_name}
          {user.data.primary_email ? ` (${user.data.primary_email})` : ""}
        </p>
      ) : null}
      {needsSignIn ? (
        <p role="alert" className="mt-3">
          For your safety this needs a recent sign-in.{" "}
          <Link className="text-primary underline" to="/login?return_to=/account">Sign in again</Link>, then retry.
        </p>
      ) : null}

      <section aria-labelledby="data-title" className="mt-6 rounded-lg border border-border bg-surface p-4">
        <h2 id="data-title" className="text-lg font-medium">Your data</h2>
        <p className="mt-1">
          Download your profile, sign-in methods, workspaces, projects and the builds you requested as JSON. Source
          and explanations are exported per build from its Overview.
        </p>
        <button type="button" onClick={() => download.mutate()} disabled={download.isPending}
          className="mt-3 h-10 rounded-lg border border-input-border px-4 font-medium">
          Download my data
        </button>
        {download.error && !needsSignIn ? (
          <p role="alert" className="mt-2 text-destructive">Could not prepare the download.</p>
        ) : null}
      </section>

      <ConfirmDeletion
        title="Delete account"
        expected={ACCOUNT_CONFIRMATION}
        expectedLabel="the phrase"
        actionLabel="Delete my account"
        pending={remove.isPending || unresolved}
        error={needsSignIn || sole.length ? null : remove.error}
        onConfirm={() => remove.mutate()}
      >
        <p>
          You are signed out everywhere and leave every workspace. Workspaces you own alone must get another owner
          first, or be deleted with your account.
        </p>
        {sole.length ? (
          <fieldset className="mt-2 space-y-1" aria-describedby="sole-help">
            <legend className="font-medium">Workspaces you own alone</legend>
            <p id="sole-help" className="text-sm text-muted">
              Select each one to delete it with your account, or make someone else an owner and retry.
            </p>
            {sole.map((w) => (
              <label key={w.id} className="flex items-center gap-2">
                <input type="checkbox" checked={chosen[w.id] ?? false}
                  onChange={(e) => setChosen((c) => ({ ...c, [w.id]: e.target.checked }))} />
                Delete {w.name} <span className="font-mono text-muted">({w.slug})</span>
              </label>
            ))}
          </fieldset>
        ) : null}
      </ConfirmDeletion>
    </section>
  );
}
