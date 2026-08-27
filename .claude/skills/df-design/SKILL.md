---
name: df-design
description: Pre-flight for the Claude Code /design artboard workflow in DF Consulting apps (Flex, LINDA, R3CRUIT3R, tactics board). Captures the repo's design tokens into CLAUDE.md so every artboard stays on-style, composes a complete design brief, then hands off to /design. Use before building any customer-facing screen, when the user says "design it", or when /design output drifts from the app's look.
argument-hint: [screen or feature to design]
---

# DF Design pre-flight

`/design` reads the codebase and matches the UI style, but it does best when the tokens and conventions are stated in CLAUDE.md and the brief is specific. This skill does both, then invokes `/design`.

## Step 1: Make sure CLAUDE.md has a Design block

Check the repo's `CLAUDE.md` for a `## Design` section. If it is missing or stale, build it from the codebase:

- Colors: read `tailwind.config.*`, `app/globals.css` (shadcn CSS variables: `--primary`, `--background`, `--foreground`, `--muted`, `--destructive`, `--radius`), and any theme or branding table in Prisma (Flex tenants carry their own brand colors; note which values are tenant-overridable).
- Type: font imports in `app/layout.tsx` (next/font), base size, heading scale.
- Spacing and radius: Tailwind spacing scale in use, `--radius` value, card padding convention.
- Components: list the shadcn/ui components already installed under `components/ui/`, plus any app-level components under `components/` that a new screen should reuse (page shell, data table, empty state, form layout, AI component folder).
- Layout: app shell structure (sidebar or top nav, content max width, mobile breakpoints).
- Rules: multi-tenant white-label constraints (Flex: no hardcoded brand colors or logos in screens, everything reads from tenant theme), feature-gate placement, loading and empty states required.

Write the section in the format in `claude-md-design-block.md` and commit it. Keep it under 40 lines. This is the single highest-leverage thing for consistent artboards.

## Step 2: Compose the brief

Take `$ARGUMENTS` (or ask what screen if empty). Look up a matching brief in `design-briefs.md`. If one exists, use it as the base and update it with anything the user said. If none exists, write one in the same shape:

- Screen name and route
- Who uses it and on what device (gym owner on desktop, member on phone, coach on iPad)
- The one job the screen must do
- Data on screen (name the Prisma models and fields)
- Primary action, secondary actions
- States: loading, empty, error, feature-gated
- Components to reuse (from the Design block)
- Constraints (white-label safe, mobile first, matches existing screen X)
- Ask for 3 to 4 options that differ in layout, not just color

## Step 3: Hand off to /design

Invoke `/design` with the composed brief as the argument. Tell the user the canvas link will print, and that the loop from here is:

1. Pick an artboard.
2. Highlight and edit the specific region they dislike instead of re-prompting the whole design.
3. Say "implement option N" when satisfied.

## Step 4: After implementation

Before the user commits, remind them to run `/df-ship-check`. If the new screen introduced a color, font, or spacing value not in the Design block, add it to CLAUDE.md so the next artboard inherits it.

## Notes

- `/design` is a research preview and needs Claude Code v2.1.233 or later. If it is not recognized, run `claude update`.
- Skip this for internal admin pages. Every customer-facing screen gets it.
- The Flex Design block should be authored once and copied into LINDA, since LINDA is being repositioned as a Flex add-on and must look like part of Flex.
