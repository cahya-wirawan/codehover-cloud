import type { ReactNode } from "react";
import { useId } from "react";

import { cn } from "../../lib/cn";

/** A titled region of a page: a heading, an optional description and actions, then content. Hairline
 * separation instead of a card per section keeps pages quiet (17). */
export function Section({
  title,
  description,
  actions,
  children,
  className,
  tone,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
  tone?: "danger";
}) {
  const id = useId();
  return (
    <section
      aria-labelledby={id}
      className={cn(
        "border-t border-border pt-6",
        tone === "danger" && "rounded-[var(--radius-overlay)] border border-destructive/40 p-5",
        className,
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-[64ch]">
          <h2 id={id} className="text-[15px] font-semibold">
            {title}
          </h2>
          {description ? <div className="mt-1 text-muted">{description}</div> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {children ? <div className="mt-4">{children}</div> : null}
    </section>
  );
}

/** Page title row: breadcrumb-free; the top bar carries location. */
export function PageHeader({
  title,
  description,
  actions,
  meta,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  meta?: ReactNode;
}) {
  return (
    <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="truncate text-[22px] leading-8 font-semibold">{title}</h1>
        {meta ? <div className="mt-1.5 flex flex-wrap items-center gap-2 text-muted">{meta}</div> : null}
        {description ? <p className="mt-2 max-w-[68ch] text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/** An empty state names the next useful action (17). */
export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-[var(--radius-overlay)] border border-dashed border-input-border/60 px-6 py-10 text-center">
      <p className="font-medium">{title}</p>
      {children ? <div className="mx-auto mt-1 max-w-[52ch] text-muted">{children}</div> : null}
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  );
}
