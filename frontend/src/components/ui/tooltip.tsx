import { Tooltip as Tip } from "radix-ui";
import type { ReactNode } from "react";

/** Non-interactive hints only (17); anything a reader can act on is a popover or dialog. */
export function Tooltip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Tip.Provider delayDuration={300}>
      <Tip.Root>
        <Tip.Trigger asChild>{children}</Tip.Trigger>
        <Tip.Portal>
          <Tip.Content
            sideOffset={6}
            className="z-50 rounded-[4px] bg-foreground px-2 py-1 text-xs text-background shadow-[var(--shadow-overlay)]"
          >
            {label}
          </Tip.Content>
        </Tip.Portal>
      </Tip.Root>
    </Tip.Provider>
  );
}
