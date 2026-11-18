import { isRouteErrorResponse, Link, useRouteError } from "react-router";

import { ServiceStatus } from "../features/status/ServiceStatus";

const SAMPLE = [
  ["kw", "def "],
  ["name", "resolve_commit"],
  ["p", "(repo, ref):"],
] as const;

/** The product in one picture: a definition, its quiet underline, and the card it opens (S01). */
function AnnotatedExample() {
  return (
    <figure aria-label="Example: a definition and its explanation card" className="relative">
      <div className="panel overflow-hidden font-mono text-[13px] leading-[22px]">
        <div className="flex h-9 items-center border-b border-border px-4 text-xs text-muted">acquire/snapshot.py</div>
        <pre className="px-4 py-3">
          <span className="token comment">{"# Pin the build to one exact commit."}</span>
          {"\n"}
          {SAMPLE.map(([kind, text]) =>
            kind === "name" ? (
              <span key={text} className="rounded-[3px] bg-primary/10 underline decoration-annotation decoration-[1.5px] underline-offset-[5px]">
                {text}
              </span>
            ) : (
              <span key={text} className={kind === "kw" ? "token keyword" : "token punctuation"}>
                {text}
              </span>
            ),
          )}
          {"\n    sha = repo.commit(ref).sha\n    "}
          <span className="token keyword">return</span>
          {" Snapshot(repo, sha)"}
        </pre>
      </div>
      <div className="relative -mt-6 ml-10 w-[min(100%,24rem)] rounded-[var(--radius-overlay)] border border-border bg-surface p-4 shadow-[var(--shadow-overlay)] sm:ml-24">
        <p className="flex items-center gap-2">
          <span className="rounded-full border border-primary/30 bg-primary/10 px-2 text-xs font-medium text-primary">function</span>
          <span className="font-mono text-[13px] font-medium">resolve_commit</span>
        </p>
        <p className="mt-2 leading-[22px]">
          Turns a branch or tag into the exact commit a build reads, so every explanation points at code that cannot
          change underneath it.
        </p>
        <p className="mt-2 text-xs text-muted">Used by start_build · matched by name</p>
      </div>
    </figure>
  );
}

export function HomePage() {
  return (
    <div className="mx-auto max-w-6xl py-6">
      <section aria-labelledby="home-title" className="grid items-center gap-12 lg:grid-cols-[1fr_minmax(0,30rem)]">
        <div className="max-w-[34rem]">
          <h1 id="home-title" className="text-[34px] leading-[42px] font-semibold tracking-[-0.02em]">
            Understand any repository through cited, browsable explanations
          </h1>
          <p className="mt-4 text-[15px] leading-[26px] text-muted">
            Connect a GitHub repository and build a private site of it: every definition explained where it is
            defined, a guide that cites the exact lines it relies on, and a reference graph to follow.
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Link to="/w" className="btn btn-primary btn-lg">
              Go to your workspaces
            </Link>
          </div>
          <div className="mt-8 text-[13px] text-muted">
            <ServiceStatus />
          </div>
        </div>
        <AnnotatedExample />
      </section>
      <section aria-labelledby="how-title" className="mt-20 border-t border-border pt-8">
        <h2 id="how-title" className="section-title">How a site is made</h2>
        <ol className="mt-5 grid gap-8 md:grid-cols-3">
          {[
            ["Connect", "Grant read-only access to the repositories you choose. Nothing else in your GitHub account is visible."],
            ["Build", "A snapshot is pinned to one commit, scanned for secrets, parsed in a sandbox and explained within the cap you set."],
            ["Read and share", "Browse the code with explanations in place. Sites stay private until an admin publishes them."],
          ].map(([title, text], index) => (
            <li key={title}>
              <p className="flex items-baseline gap-3">
                <span className="font-mono text-xs text-muted">{index + 1}</span>
                <span className="font-semibold">{title}</span>
              </p>
              <p className="mt-2 pl-6 leading-[22px] text-muted">{text}</p>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <section aria-labelledby="nf-title">
      <h1 id="nf-title" className="text-[22px] leading-8 font-semibold">
        Page not found
      </h1>
      <p className="mt-3 text-muted">
        <Link to="/" className="text-primary underline underline-offset-2">
          Go to the start page
        </Link>
      </p>
    </section>
  );
}

export function RouteError() {
  const error = useRouteError();
  const title = isRouteErrorResponse(error) ? `${error.status} ${error.statusText}` : "Something went wrong";
  return (
    <main id="main" className="mx-auto max-w-2xl px-6 py-8">
      <h1 className="text-[22px] leading-8 font-semibold">{title}</h1>
      <p className="mt-3 text-muted">Reload the page or return to the start page.</p>
    </main>
  );
}
