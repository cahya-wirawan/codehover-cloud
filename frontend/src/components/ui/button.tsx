import { Slot } from "radix-ui";
import type { ComponentProps } from "react";

import { cn } from "../../lib/cn";
import { buttonVariants, type ButtonVariants } from "./button-variants";

export function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: ComponentProps<"button"> & ButtonVariants & { asChild?: boolean }) {
  const Component = asChild ? Slot.Root : "button";
  return <Component className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
