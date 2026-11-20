import { useQuery } from "@tanstack/react-query";
import { FileWarning, LifeBuoy, Mail } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { api, type SiteMeta } from "../../lib/api/resources";

/** Policy texts must match the actual retention and provider configuration (08 "Launch review
 * checklist"). These are drafts: a jurisdiction-specific legal review comes before a public paid launch. */
function useMeta() {
  return useQuery({ queryKey: ["meta"], queryFn: api.meta, staleTime: 5 * 60_000 }).data;
}

function operator(meta: SiteMeta | undefined): string {
  return meta?.operator_name ?? "The operator of this service";
}

function Draft() {
  return (
    <p role="note" className="mb-8 flex gap-2.5 rounded-[var(--radius-overlay)] border border-warning/50 bg-warning/5 px-4 py-3">
      <FileWarning aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
      <span>
        <span className="font-medium">Draft for legal review.</span>{" "}
        <span className="text-muted">
          This text describes how the service actually works today. It is not yet reviewed for any jurisdiction and
          may change before the public launch.
        </span>
      </span>
    </p>
  );
}

function Doc({ id, title, updated, children }: { id: string; title: string; updated: string; children: ReactNode }) {
  return (
    <article aria-labelledby={id} className="mx-auto max-w-[68ch] py-2">
      <h1 id={id} className="text-[28px] leading-9 font-semibold tracking-[-0.01em]">
        {title}
      </h1>
      <p className="mt-2 mb-6 text-[13px] text-muted">Last updated {updated}</p>
      <Draft />
      <div className="legal flex flex-col gap-6 leading-[24px]">{children}</div>
    </article>
  );
}

function Part({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2.5">
      <h2 className="text-[17px] font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function Contact({ meta }: { meta: SiteMeta | undefined }) {
  return meta?.support_email ? (
    <a className="text-primary underline underline-offset-2" href={`mailto:${meta.support_email}`}>
      {meta.support_email}
    </a>
  ) : (
    <Link className="text-primary underline underline-offset-2" to="/support">
      the support page
    </Link>
  );
}

export function PrivacyPage() {
  const meta = useMeta();
  return (
    <Doc id="privacy-title" title="Privacy notice" updated="November 2026">
      <p>
        {operator(meta)} runs this service. This notice explains what we collect when you sign in, connect a
        repository and build a site, what we do with it, how long we keep it and how to have it deleted.
      </p>
      <Part title="What we collect">
        <ul className="ml-5 list-disc space-y-1.5">
          <li>
            <span className="font-medium">Your account:</span> the identifier, name and email address that Google or
            GitHub returns when you sign in, and your sessions. We store only a digest of each session cookie.
          </li>
          <li>
            <span className="font-medium">Repositories you grant:</span> a snapshot of each repository at one commit,
            fetched with read-only access to the repositories you selected. Files that look like secrets are excluded
            before anything else reads them.
          </li>
          <li>
            <span className="font-medium">What a build produces:</span> explanations, guide pages, search indexes and
            the usage ledger of model calls.
          </li>
          <li>
            <span className="font-medium">Billing:</span> the payment provider&apos;s customer and subscription
            identifiers and the subscription state. Card details, billing addresses and invoices stay with the payment
            provider and never reach our servers.
          </li>
          <li>
            <span className="font-medium">Records:</span> an audit log of changes in a workspace and operational
            metrics that contain counts and identifiers, not source code.
          </li>
        </ul>
      </Part>
      <Part title="How we use it">
        <p>
          To run the service you asked for: build and show your sites, enforce workspace permissions, charge the plan
          you chose and keep the service secure. Repository content is sent to the model provider of the preset you
          choose only to generate that build&apos;s explanations; it is not used to train models. We do not sell data or
          use it for advertising. The service sets only the cookies it needs to keep you signed in and to protect
          forms; there are no analytics or advertising cookies.
        </p>
      </Part>
      <Part title="Who processes it for us">
        <ul className="ml-5 list-disc space-y-1.5">
          <li>Google and GitHub, when you sign in with them.</li>
          <li>GitHub, to read the repositories you grant.</li>
          <li>
            The model provider named by the preset you approve before each build: Anthropic, OpenAI, Google (Gemini), or
            OpenRouter and the host it routes that model to. With your own key, that provider&apos;s terms with you apply.
          </li>
          <li>Stripe, for checkout, subscriptions, invoices and payment methods.</li>
          <li>The hosting and storage providers of this deployment.</li>
        </ul>
      </Part>
      <Part title="How long we keep it">
        <ul className="ml-5 list-disc space-y-1.5">
          <li>Failed, cancelled or over-budget builds and rejected snapshots: 7 days.</li>
          <li>
            Ready builds that are neither published nor among a project&apos;s five newest: 30 days. Build events and
            unused estimates: 30 days.
          </li>
          <li>Exports: 24 hours after they are created.</li>
          <li>The audit log: 180 days.</li>
          <li>
            After a subscription ends, sites and exports stay readable for 30 days; then published sites go offline.
            Nothing is deleted because a payment failed.
          </li>
          <li>Backups age out within 35 days.</li>
        </ul>
      </Part>
      <Part title="Deleting and exporting">
        <p>
          Owners can delete a project or a workspace, and you can delete your account, from the service itself.
          Deletion takes effect at once: sites stop being served and running builds stop. Content is purged within
          minutes and at most within 24 hours; a restored backup replays every deletion before it serves traffic. Copies
          that people already downloaded or saw on a public site cannot be recalled. You can download an export of your
          account data from the account page at any time.
        </p>
      </Part>
      <Part title="Contact">
        <p>
          For questions or requests about your data, write to <Contact meta={meta} />.
        </p>
      </Part>
    </Doc>
  );
}

export function TermsPage() {
  const meta = useMeta();
  return (
    <Doc id="terms-title" title="Terms of service" updated="November 2026">
      <p>
        These terms apply between you and {operator(meta)} when you use the service. The service is meant for
        professional developers and organizations, not for children.
      </p>
      <Part title="Your repositories and sites">
        <p>
          You keep every right in your code. By connecting a repository you confirm you may let the service process it,
          and you give us permission to do so only to build and show the sites you ask for. Sites are private to your
          workspace. Publishing one makes it readable by anyone with the address, which an admin confirms each time;
          you are responsible for having the right to publish that code and its explanations.
        </p>
      </Part>
      <Part title="Generated explanations">
        <p>
          Explanations and guides are written by a language model and checked against the cited source, but they can
          still be wrong or incomplete. Check the cited lines before relying on them. Sites show which text was
          generated.
        </p>
      </Part>
      <Part title="Plans and payment">
        <p>
          Paid plans renew monthly through Stripe until you cancel them in the billing portal; a cancelled plan runs
          until the end of the paid period. Each plan includes a monthly budget for model use, measured in US dollars
          at the model provider&apos;s cost. It is not credit, does not carry over and cannot be paid out. There are
          no automatic overage charges: when the budget is used up, builds wait until the next period or an upgrade.
          The trial is available once per workspace. If a payment fails, new builds pause; reading continues for 7
          days before published sites go offline.
        </p>
      </Part>
      <Part title="Acceptable use">
        <p>
          Do not connect code you have no right to process, use the service to attack others or its own security, or
          publish content that is unlawful. We may take a published site offline or suspend a workspace that breaks
          these terms, and we record every such action.
        </p>
      </Part>
      <Part title="Availability and liability">
        <p>
          We work to keep the service available and your data safe, and we tell affected workspaces about incidents
          that concern them. To the extent the law allows, the service is provided as it is, and our liability is
          limited to the amount you paid in the 12 months before the claim.
        </p>
      </Part>
      <Part title="Changes and contact">
        <p>
          We announce material changes to these terms in the service before they apply. Questions go to{" "}
          <Contact meta={meta} />.
        </p>
      </Part>
    </Doc>
  );
}

export function SupportPage() {
  const meta = useMeta();
  return (
    <article aria-labelledby="support-title" className="mx-auto max-w-[68ch] py-2">
      <h1 id="support-title" className="flex items-center gap-2.5 text-[28px] leading-9 font-semibold tracking-[-0.01em]">
        <LifeBuoy aria-hidden="true" className="size-6 text-muted" />
        Support
      </h1>
      <p className="mt-3 text-muted">
        Include the workspace and the build or request ID that the page shows, never passwords, tokens or source code.
      </p>
      <div className="mt-6 flex flex-col gap-6 leading-[24px]">
        {meta?.support_email ? (
          <p className="flex items-center gap-2">
            <Mail aria-hidden="true" className="size-4 text-muted" />
            <a className="text-primary underline underline-offset-2" href={`mailto:${meta.support_email}`}>
              {meta.support_email}
            </a>
          </p>
        ) : (
          <p className="text-muted">The operator of this deployment has not published a support address yet.</p>
        )}
        <Part title="Payments and plans">
          <p>
            Owners manage payment methods, invoices and cancellation from the workspace&apos;s Plan and usage page. If
            a payment went through but the plan did not change within an hour, write to support with the workspace
            name: we reconcile with the payment provider, and returning from checkout never changes a plan by itself.
          </p>
        </Part>
        <Part title="When someone needs to look inside your workspace">
          <p>
            Our staff cannot read your code or sites unless a workspace admin grants time-limited, read-only support
            access from the workspace page. Every grant and every operator action is in your audit log.
          </p>
        </Part>
        <Part title="Reporting a security issue">
          <p>
            Report vulnerabilities to the support address with &quot;security&quot; in the subject. Please do not test
            against other people&apos;s workspaces.
          </p>
        </Part>
      </div>
    </article>
  );
}
