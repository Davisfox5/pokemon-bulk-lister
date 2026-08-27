# Template: `## Design` block for CLAUDE.md

Fill from the codebase. Replace every bracketed value. Delete lines that do not apply. Keep under 40 lines so it stays cheap in context.

```markdown
## Design

Stack: Next.js App Router, Tailwind, shadcn/ui. Every new screen reuses shadcn primitives from `components/ui/` and app components listed below. Do not introduce new UI libraries.

### Tokens (from app/globals.css and tailwind.config)
- Primary: [hsl or hex]  (Flex: tenant-overridable via `Tenant.theme.primary`, never hardcode)
- Background / foreground: [values]
- Muted / border: [values]
- Destructive: [value]
- Radius: [--radius value]
- Font: [family via next/font], base [size], headings [scale]
- Spacing: cards use [p-6], page gutter [px-4 md:px-8], content max width [max-w-6xl]

### App shell
- [Sidebar nav on desktop, bottom tabs on mobile] / [top nav]
- Page header pattern: [component name] with title, description, primary action right-aligned
- Data tables use [components/data-table.tsx] with server-side pagination
- Forms use react-hook-form + zod with [components/form-layout.tsx]
- Empty state component: [components/empty-state.tsx]
- AI-generated content renders inside [components/ai/*] with a visible "AI" label

### Rules
- White-label safe: no product name, logo, or brand color hardcoded in screens (Flex, LINDA).
- Every screen ships loading, empty, and error states.
- Feature-gated screens show [components/upgrade-prompt.tsx], not a blank page.
- Mobile first for member-facing and recruit-facing screens; desktop first for owner and coach admin.
- Match existing screen [route] when in doubt.
```
