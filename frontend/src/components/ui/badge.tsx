import type { ComponentProps, ReactNode } from "react";

import { cn } from "../../lib/cn";

const TONES = {
  neutral: "border-border bg-surface-muted text-muted",
  primary: "border-primary/30 bg-primary/10 text-primary",
  success: "border-success/30 bg-success/10 text-success",
  warning: "border-warning/40 bg-warning/10 text-warning",
  destructive: "border-destructive/30 bg-destructive/10 text-destructive",
} as const;

/** Status badges pair an icon with words: never colour alone (17). */
export function Badge({
  tone = "neutral",
  icon,
  className,
  children,
  ...props
}: ComponentProps<"span"> & { tone?: keyof typeof TONES; icon?: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex h-6 items-center gap-1 rounded-full border px-2 text-xs font-medium [&_svg]:size-3.5",
        TONES[tone],
        className,
      )}
      {...props}
    >
      {icon}
      {children}
    </span>
  );
}
