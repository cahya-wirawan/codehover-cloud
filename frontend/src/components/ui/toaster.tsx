import { Toaster as Sonner } from "sonner";

/** Toasts acknowledge success; persistent problems stay inline (17). */
export function Toaster() {
  return (
    <Sonner
      position="bottom-right"
      toastOptions={{
        classNames: {
          toast:
            "!rounded-[var(--radius-overlay)] !border !border-border !bg-surface !text-foreground !shadow-[var(--shadow-overlay)] !font-sans",
          description: "!text-muted",
        },
      }}
    />
  );
}
