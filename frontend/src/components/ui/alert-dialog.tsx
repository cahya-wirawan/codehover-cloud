import { AlertDialog as Alert } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";

import { cn } from "../../lib/cn";

export const AlertDialog = Alert.Root;
export const AlertDialogTrigger = Alert.Trigger;
export const AlertDialogCancel = Alert.Cancel;
export const AlertDialogAction = Alert.Action;

export function AlertDialogContent({
  title,
  description,
  children,
  className,
  ...props
}: ComponentProps<typeof Alert.Content> & { title: string; description: ReactNode }) {
  return (
    <Alert.Portal>
      <Alert.Overlay className="fixed inset-0 z-50 bg-foreground/30" />
      <Alert.Content
        className={cn(
          "fixed top-1/2 left-1/2 z-50 w-[min(92vw,30rem)] -translate-x-1/2 -translate-y-1/2 rounded-[var(--radius-overlay)] border border-border bg-surface p-5 shadow-[var(--shadow-overlay)]",
          className,
        )}
        {...props}
      >
        <Alert.Title className="text-base font-semibold">{title}</Alert.Title>
        <Alert.Description className="mt-2 text-muted">{description}</Alert.Description>
        <div className="mt-4">{children}</div>
      </Alert.Content>
    </Alert.Portal>
  );
}
