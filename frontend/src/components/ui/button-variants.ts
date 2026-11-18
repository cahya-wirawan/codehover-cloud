import { cva, type VariantProps } from "class-variance-authority";

export const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-[var(--radius-control)] font-medium transition-colors duration-150 disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-primary text-primary-foreground hover:bg-primary/90",
        secondary: "border border-input-border bg-surface text-foreground hover:bg-surface-muted",
        ghost: "text-foreground hover:bg-surface-muted",
        destructive: "border border-destructive bg-surface text-destructive hover:bg-destructive/10",
        link: "h-auto px-0 text-primary underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-8 px-3 text-[13px]",
        md: "h-9 px-3.5",
        lg: "h-10 px-4",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export type ButtonVariants = VariantProps<typeof buttonVariants>;
