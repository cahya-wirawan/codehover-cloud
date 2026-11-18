import { cloneElement, type ComponentProps, type ReactElement, useId } from "react";

import { cn } from "../../lib/cn";

export const fieldClass =
  "h-9 w-full rounded-[var(--radius-control)] border border-input-border bg-surface px-3 text-foreground placeholder:text-muted disabled:opacity-60 aria-[invalid=true]:border-destructive";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(fieldClass, className)} {...props} />;
}

export function Select({ className, ...props }: ComponentProps<"select">) {
  return <select className={cn(fieldClass, "pr-8", className)} {...props} />;
}

/** A visible label above its control, with an optional hint linked by aria-describedby, so the label alone
 * is the control's name (17: forms have visible labels). */
export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: string;
  children: ReactElement<{ "aria-describedby"?: string }>;
  className?: string;
}) {
  const hintId = useId();
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium">{label}</span>
        {hint ? cloneElement(children, { "aria-describedby": hintId }) : children}
      </label>
      {hint ? (
        <span id={hintId} className="text-xs text-muted">
          {hint}
        </span>
      ) : null}
    </div>
  );
}
