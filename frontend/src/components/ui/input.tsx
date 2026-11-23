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

/** A visible label above its control, with an optional hint and error linked by aria-describedby, so the
 * label alone is the control's name (17: forms have visible labels). An error marks the control invalid. */
export function Field({
  label,
  hint,
  error,
  children,
  className,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  children: ReactElement<{ "aria-describedby"?: string; "aria-invalid"?: boolean }>;
  className?: string;
}) {
  const hintId = useId();
  const errorId = useId();
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ");
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium">{label}</span>
        {describedBy ? cloneElement(children, { "aria-describedby": describedBy, ...(error ? { "aria-invalid": true } : {}) }) : children}
      </label>
      {error ? (
        <span id={errorId} role="alert" className="text-xs text-destructive">
          {error}
        </span>
      ) : null}
      {hint ? (
        <span id={hintId} className="text-xs text-muted">
          {hint}
        </span>
      ) : null}
    </div>
  );
}
