# Design system — elegant professional developer SaaS

Visual direction: a precise technical workspace with generous spacing, quiet neutral surfaces, crisp typography and one restrained indigo accent. Code and evidence are the focus. Avoid decorative gradients, excessive cards, cartoon graphics and noisy dashboards. Marketing can use a large annotated-code example with a short explanation of the workflow. Workspace UI uses table/list density, clean alignment and subtle separators.

| Token | Light | Dark |
|---|---|---|
| background | #F7F8FA | #0D1117 |
| surface | #FFFFFF | #151B24 |
| surface-muted | #EEF1F5 | #1D2633 |
| foreground | #172033 | #E8EDF5 |
| text-muted | #536176 | #AAB6C8 |
| border | #D9E0EA | #354155 |
| input-border | #7C879A | #6B7A90 |
| primary | #4338CA | #A5B4FC |
| primary foreground | #FFFFFF | #17143E |
| success | #166534 | #86EFAC |
| warning | #854D0E | #FDE68A |
| destructive | #B91C1C | #FCA5A5 |

Use semantic CSS custom properties mapped through Tailwind; verify contrast per component/state rather than assuming palette compliance. Text tokens meet 4.5:1 on background, surface and surface-muted in both themes. `border` (about 1.3:1 light, 1.7:1 dark) is for decorative separators only; form fields, checkboxes and other controls whose boundary identifies them use `input-border` (at least 3:1 on every surface, WCAG 1.4.11). Self-host Inter or an equivalent readable sans for UI and JetBrains Mono for code, with system fallbacks. UI text 14px/20px default; content prose 16px/26px; code 13px/21px scalable; headings 24–32px app, 48–64px desktop marketing. Medium weight for controls/headings, no thin text. Spacing 4/8/12/16/24/32/48; radius controls 8px, panels 12px; 1px border; shadows only for floating layers.

Desktop shell: sidebar 232px, top bar 64px, max-width content 1440px with 24–32px gutters. Project list is a compact table with repository, build, age, visibility and next action. Primary action “Create project” or “Build documentation” appears once per context. Build overview groups timeline, spend/coverage and safe diagnostics. Viewer is resizable tree/code/details; at <1024px detail pane becomes sheet, at <768px tree is drawer. Mobile hovers open a bottom sheet via tap. Source code keeps LTR regardless of prose locale.

As implemented in the design pass:
- Owned shadcn/ui-style components in `frontend/src/components/ui/` built on Radix (`radix-ui`): Button, Badge, Input/Select/Field, DropdownMenu, AlertDialog, Tooltip, Skeleton, Section/PageHeader/EmptyState, and Sonner toasts. Resizable panes use `react-resizable-panels`.
- Lucide icons, aria-hidden, beside words.
- IBM Plex Sans and Mono, self-hosted through `@fontsource` (no font CDN, so the CSP stays `self`).
- Syntax colours derived from the palette: keywords in the accent, strings in success, numbers in warning, comments muted.
- The app shell: a 232 px sidebar with the workspace switcher and navigation, a 64 px top bar with breadcrumbs and the account menu (theme choice kept as the only stored preference), and a drawer below 1024 px.

shadcn/ui primitives: Button, Dialog/AlertDialog, DropdownMenu, Tabs, Sheet, Command, Tooltip for noninteractive hints, Popover for citation previews, custom accessible pinned symbol dialog, Table, Badge, Progress, Skeleton, Sonner toast, Resizable panes. Use Lucide 16/20px icons with text labels for unfamiliar actions. Status badges combine icon + words; supported/partial/unsupported/unchecked labels are never color-only. Charts need useful axes and data table fallback; no decorative circular progress.

Interaction timing 120–180ms; respect prefers-reduced-motion, no page entrance animation delaying work. Focus ring 2px primary plus offset; target size ≥24px and prefer 40px controls. Destructive dialogs describe consequence and specific project. Toasts acknowledge success; persistent problems are inline. Forms have visible labels, field-level errors and stable layout. Command shortcuts `/` search, Ctrl/Cmd+K global, Esc close, Enter pin; do not intercept typing in inputs/code selection. Empty states name the next useful action. Copywriting says “AI-generated; check cited source” and “Matched by name” precisely, without unsupported accuracy claims.

Required design review artifacts during implementation: light/dark desktop project list, build detail, code with pinned card, guide/citation preview, mobile reader, estimate and publish dialogs. Evaluate real-looking but synthetic content, long symbol names, narrow panes, 200% zoom, disabled/error/loading states and screen-reader focus. This package defines design intent and tokens, not a rendered/tested frontend.
