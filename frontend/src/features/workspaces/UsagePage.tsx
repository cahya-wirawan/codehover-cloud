import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, CircleDashed, ExternalLink, Loader2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useParams, useSearchParams } from "react-router";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { PageHeader, Section } from "../../components/ui/section";
import { ProviderKeyPanel } from "./ProviderKeyPanel";
import { ApiError } from "../../lib/api/client";
import { api, formatUsd, type BillingState, type PlanInfo } from "../../lib/api/resources";

const CONFIRM_WINDOW_MS = 120_000;
const PAID: PlanInfo["key"][] = ["pro", "team"];

const STATUS: Record<string, { label: string; tone: "success" | "warning" | "destructive" | "neutral" }> = {
  trial: { label: "Trial", tone: "success" },
  trialing: { label: "Trialing", tone: "success" },
  active: { label: "Active", tone: "success" },
  past_due: { label: "Payment overdue", tone: "warning" },
  unpaid: { label: "Unpaid", tone: "destructive" },
  canceled: { label: "Ended", tone: "neutral" },
  incomplete: { label: "Payment incomplete", tone: "warning" },
  incomplete_expired: { label: "Checkout expired", tone: "neutral" },
  paused: { label: "Paused", tone: "neutral" },
};

function date(value: string | null): string {
  return value ? new Date(value).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
}

function message(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/** Leave for the provider's page. Only https URLs from our own API are followed. */
function go(url: string) {
  if (url.startsWith("https://")) window.location.assign(url);
}

function Notice({ tone, title, children }: { tone: "warning" | "info"; title: string; children: ReactNode }) {
  return (
    <div
      role={tone === "warning" ? "alert" : "status"}
      className={
        tone === "warning"
          ? "flex gap-3 rounded-[var(--radius-overlay)] border border-warning/50 bg-warning/5 px-4 py-3"
          : "flex gap-3 rounded-[var(--radius-overlay)] border border-border bg-surface-muted px-4 py-3"
      }
    >
      {tone === "warning" ? (
        <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
      ) : (
        <Loader2 aria-hidden="true" className="mt-0.5 size-4 shrink-0 animate-spin text-muted motion-reduce:animate-none" />
      )}
      <div>
        <p className="font-medium">{title}</p>
        <div className="mt-0.5 text-muted">{children}</div>
      </div>
    </div>
  );
}

function CurrentPlan({ billing }: { billing: BillingState }) {
  const plan = billing.plans.find((p) => p.key === billing.plan);
  const status = billing.status ? STATUS[billing.status] : undefined;
  if (!plan) {
    return (
      <p className="text-muted">
        {billing.status === "canceled" && billing.enforced
          ? "The subscription has ended. Sites and exports stay readable for 30 days after it ends; choose a plan to keep them online."
          : billing.status === "canceled"
            ? "The subscription has ended. The beta allowance below applies until you choose a plan again."
          : billing.enforced
            ? "No plan yet. Start the trial or choose a plan to create projects and invite members."
            : "No plan yet. The beta allowance below applies until you choose one."}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="flex flex-wrap items-center gap-2">
        <span className="text-[15px] font-semibold">{plan.name}</span>
        {status ? (
          <Badge tone={status.tone} icon={status.tone === "success" ? <CheckCircle2 aria-hidden="true" /> : <CircleDashed aria-hidden="true" />}>
            {status.label}
          </Badge>
        ) : null}
      </p>
      <dl className="grid max-w-2xl grid-cols-2 gap-x-8 gap-y-2 sm:grid-cols-4">
        {[
          ["Included each month", formatUsd(plan.allowance_micro_usd)],
          ["Seats", String(plan.seats)],
          ["Projects", String(plan.projects)],
          ["Builds at once", String(plan.active_builds)],
        ].map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-muted">{label}</dt>
            <dd className="font-mono text-[13px]">{value}</dd>
          </div>
        ))}
      </dl>
      {billing.plan === "trial" && billing.trial_ends_at ? (
        <p className="text-muted">The trial ends on {date(billing.trial_ends_at)}. Its allowance is included once.</p>
      ) : billing.cancel_at_period_end && billing.period_end ? (
        <p className="text-muted">Cancelled: the plan continues until {date(billing.period_end)}, then ends.</p>
      ) : billing.period_end ? (
        <p className="text-muted">Renews on {date(billing.period_end)}.</p>
      ) : null}
    </div>
  );
}

/** Plan and usage (02 S16, 11): the plan, its state, upgrade and payment management for owners, and this
 * month's generation allowance. Coming back from Checkout shows a waiting state: the plan changes only
 * when the payment provider confirms it to the server (A20). */
export function UsagePage() {
  const { wid = "" } = useParams();
  const [search, setSearch] = useSearchParams();
  const [returnedAt] = useState(() => (search.get("checkout") === "returned" ? Date.now() : 0));
  const [waitedLong, setWaitedLong] = useState(false);
  useEffect(() => {
    if (!returnedAt) return;
    const timer = window.setTimeout(() => setWaitedLong(true), CONFIRM_WINDOW_MS);
    return () => window.clearTimeout(timer);
  }, [returnedAt]);
  const queryClient = useQueryClient();
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const role = workspaces.data?.items.find((w) => w.id === wid)?.role;
  const owner = role === "owner";
  const ledgerReader = role === "owner" || role === "admin"; // 06: the ledger is for owners and admins
  const billing = useQuery({
    queryKey: ["billing", wid],
    queryFn: () => api.billing(wid),
    refetchInterval: (query) => {
      const paid = query.state.data?.plan && PAID.includes(query.state.data.plan);
      return returnedAt && !paid && Date.now() - returnedAt < CONFIRM_WINDOW_MS ? 3000 : false;
    },
  });
  const usage = useQuery({ queryKey: ["usage", wid], queryFn: () => api.usage(wid) });
  const entries = useQuery({ queryKey: ["usage-entries", wid], queryFn: () => api.usageEntries(wid), enabled: ledgerReader });
  const checkout = useMutation({ mutationFn: (plan: "pro" | "team") => api.checkout(wid, plan), onSuccess: (r) => go(r.url) });
  const portal = useMutation({ mutationFn: () => api.billingPortal(wid), onSuccess: (r) => go(r.url) });
  const trial = useMutation({
    mutationFn: () => api.startTrial(wid),
    onSuccess: (state) => {
      queryClient.setQueryData(["billing", wid], state);
      void queryClient.invalidateQueries({ queryKey: ["usage", wid] });
    },
  });

  const state = billing.data;
  const paid = Boolean(state?.plan && PAID.includes(state.plan));
  const confirming = Boolean(returnedAt) && !paid;
  const timedOut = confirming && waitedLong;
  const cancelled = search.get("checkout") === "cancelled";
  const subscribed = Boolean(state?.status && ["trialing", "active", "past_due", "unpaid"].includes(state.status));
  const used = usage.data ? usage.data.settled_micro_usd + usage.data.reserved_micro_usd : 0;
  const share = usage.data?.allowance_micro_usd ? Math.min(100, (used / usage.data.allowance_micro_usd) * 100) : 0;

  return (
    <section aria-labelledby="usage-title" className="flex max-w-5xl flex-col gap-8">
      <PageHeader
        title={<span id="usage-title">Plan and usage</span>}
        description="The workspace's plan, what it includes, and the generation allowance used this month."
        actions={
          owner && state?.has_customer ? (
            <Button onClick={() => portal.mutate()} disabled={portal.isPending}>
              <ExternalLink aria-hidden="true" />
              Manage payment and invoices
            </Button>
          ) : null
        }
      />
      <div className="-mt-4 flex flex-col gap-3 empty:hidden">
        {confirming && !timedOut ? (
          <Notice tone="info" title="Confirming your payment">
            The plan changes when the payment provider confirms the payment to us, usually within a minute. You can
            leave this page; nothing is lost.
          </Notice>
        ) : null}
        {timedOut ? (
          <Notice tone="warning" title="The payment is not confirmed yet">
            If you completed checkout, the confirmation may be delayed. Reload this page later, or contact support if
            the plan does not change within an hour.
          </Notice>
        ) : null}
        {cancelled ? (
          <p role="status" className="flex items-center justify-between gap-3 text-muted">
            Checkout was cancelled; nothing changed.
            <Button variant="ghost" size="sm" onClick={() => setSearch({}, { replace: true })}>
              Dismiss
            </Button>
          </p>
        ) : null}
        {state?.generation_blocked === "past_due" ? (
          <Notice tone="warning" title="A payment is overdue">
            New builds are paused until the payment succeeds; reading and published sites continue for 7 days.
            {owner ? " Update the payment method under Manage payment and invoices." : " Ask a workspace owner to update the payment method."}
          </Notice>
        ) : null}
        {state?.generation_blocked === "disputed" ? (
          <Notice tone="warning" title="A payment is disputed">
            New builds are paused while the dispute is open. Reading continues. Contact support to resolve it.
          </Notice>
        ) : null}
        {state?.hosting_suspended ? (
          <Notice tone="warning" title="Published sites are offline">
            Sites were taken offline by the plan state. Members can still read and export every build; nothing was
            deleted. Choosing a plan or settling the payment brings them back.
          </Notice>
        ) : null}
      </div>

      <Section title="Current plan">
        {state ? <CurrentPlan billing={state} /> : <p className="text-muted">Loading the plan…</p>}
      </Section>

      {state && !subscribed ? (
        <Section
          title="Plans"
          description={
            state.configured
              ? "The payment provider shows the price, currency and taxes before you pay. Plans renew monthly and can be cancelled at any time. The included amount is a budget for model use at the model provider's cost, in US dollars; it is not credit and cannot be paid out."
              : "Paid plans are not offered on this deployment yet."
          }
        >
          <div className="panel overflow-x-auto">
            <table className="w-full text-left">
              <caption className="sr-only">Plans</caption>
              <thead>
                <tr className="border-b border-border text-xs text-muted">
                  <th scope="col" className="px-4 py-2.5 font-medium">Plan</th>
                  <th scope="col" className="px-4 py-2.5 font-medium">Included each month</th>
                  <th scope="col" className="px-4 py-2.5 font-medium">Seats</th>
                  <th scope="col" className="px-4 py-2.5 font-medium">Projects</th>
                  <th scope="col" className="px-4 py-2.5 font-medium">Builds at once</th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    <span className="sr-only">Action</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {state.plans.map((plan) => (
                  <tr key={plan.key}>
                    <th scope="row" className="px-4 py-3 font-medium">
                      {plan.name}
                      {plan.key === "trial" ? <span className="block text-xs font-normal text-muted">14 days, once</span> : null}
                    </th>
                    <td className="px-4 py-3 font-mono text-[13px]">
                      {formatUsd(plan.allowance_micro_usd)}
                      {plan.key === "trial" ? <span className="font-sans text-muted"> in total</span> : null}
                    </td>
                    <td className="px-4 py-3 font-mono text-[13px]">{plan.seats}</td>
                    <td className="px-4 py-3 font-mono text-[13px]">{plan.projects}</td>
                    <td className="px-4 py-3 font-mono text-[13px]">{plan.active_builds}</td>
                    <td className="px-4 py-3 text-right">
                      {!owner ? null : plan.key === "trial" ? (
                        state.trial_available ? (
                          <Button size="sm" onClick={() => trial.mutate()} disabled={trial.isPending}>
                            Start the trial
                          </Button>
                        ) : (
                          <span className="text-xs text-muted">{state.plan === "trial" ? "Current plan" : "Used"}</span>
                        )
                      ) : plan.available ? (
                        <Button
                          size="sm"
                          variant="primary"
                          onClick={() => checkout.mutate(plan.key as "pro" | "team")}
                          disabled={checkout.isPending || confirming}
                        >
                          Choose {plan.name}
                        </Button>
                      ) : (
                        <span className="text-xs text-muted">Not offered</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!owner ? <p className="mt-3 text-muted">Only workspace owners can change the plan.</p> : null}
          {checkout.error || trial.error || portal.error ? (
            <p role="alert" className="mt-3 text-destructive">
              {message(checkout.error ?? trial.error ?? portal.error, "That did not work. Try again.")}
            </p>
          ) : null}
        </Section>
      ) : null}

      <ProviderKeyPanel workspaceId={wid} owner={owner} />

      <Section
        title="Generation this month"
        description="Model use is charged against the allowance at the provider's cost. Reading, search and published sites never use it."
      >
        {usage.data ? (
          <div className="flex flex-col gap-4">
            <div>
              <div className="flex items-baseline justify-between gap-3">
                <p>
                  <span className="font-mono text-[15px] font-medium">{formatUsd(usage.data.available_micro_usd)}</span>
                  <span className="text-muted"> available of {formatUsd(usage.data.allowance_micro_usd)}</span>
                </p>
                <p className="text-xs text-muted">Resets on {date(usage.data.period_end)}</p>
              </div>
              <div
                role="meter"
                aria-label="Allowance used"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(share)}
                className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-muted"
              >
                <div className="h-full rounded-full bg-primary" style={{ width: `${share}%` }} />
              </div>
              <p className="mt-2 text-xs text-muted">
                Used {formatUsd(usage.data.settled_micro_usd)} · held for running builds {formatUsd(usage.data.reserved_micro_usd)}
              </p>
            </div>
            {entries.data?.items.length ? (
              <div className="panel overflow-x-auto">
                <table className="w-full text-left">
                  <caption className="sr-only">Recent ledger entries</caption>
                  <thead>
                    <tr className="border-b border-border text-xs text-muted">
                      <th scope="col" className="px-4 py-2.5 font-medium">When</th>
                      <th scope="col" className="px-4 py-2.5 font-medium">Entry</th>
                      <th scope="col" className="px-4 py-2.5 text-right font-medium">Amount</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {entries.data.items.map((e) => (
                      <tr key={e.id}>
                        <td className="px-4 py-2.5 whitespace-nowrap text-muted">{new Date(e.created_at).toLocaleString()}</td>
                        <td className="px-4 py-2.5 font-mono text-[13px]">{e.kind}</td>
                        <td className="px-4 py-2.5 text-right font-mono text-[13px]">{formatUsd(e.amount_micro_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : entries.data ? (
              <p className="text-muted">No generation this month yet.</p>
            ) : null}
          </div>
        ) : (
          <p className="text-muted">Loading usage…</p>
        )}
      </Section>
    </section>
  );
}
