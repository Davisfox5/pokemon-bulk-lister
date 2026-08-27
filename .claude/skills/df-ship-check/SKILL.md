---
name: df-ship-check
description: Pre-commit gate for DF Consulting Next.js App Router + Prisma multi-tenant apps (Flex, LINDA, R3CRUIT3R). Use before any commit or PR, when the user says "ship check" or "tenant safe?", or after implementing a /design artboard. Checks tenant scoping, feature gates, migrations, secrets, types, and required UI states, then reports pass or fail with exact fixes.
allowed-tools: Read Grep Glob Bash(npx tsc *) Bash(npx prisma validate) Bash(npm run lint*) Bash(npm run build*) Bash(git *)
---

# DF ship check

## Working tree

Changed files:

!`git status --porcelain 2>/dev/null | head -60`

Diff (staged and unstaged):

!`git diff HEAD --stat 2>/dev/null | tail -20`

New or changed Prisma queries in the diff:

!`git diff HEAD -U0 2>/dev/null | grep -E "^\+" | grep -E "prisma\.[a-zA-Z]+\.(find|create|update|delete|upsert|count|aggregate|groupBy)" | head -40`

New or changed routes:

!`git diff HEAD --name-only 2>/dev/null | grep -E "app/.*(page|route|layout)\.(ts|tsx)$" | head -30`

Schema and migration changes:

!`git diff HEAD --name-only 2>/dev/null | grep -Ei "schema\.prisma|prisma/migrations" | head -20`

Possible secrets in the diff:

!`git diff HEAD 2>/dev/null | grep -E "^\+" | grep -Ei "(sk_live|sk_test|whsec_|api[_-]?key|secret|password|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{12,}" | head -10`

## Checklist

Work through each item against the diff. Read the actual files; do not judge from the grep alone.

### 1. Tenant scoping (Flex and LINDA are multi-tenant; R3CRUIT3R is scoped per program)
- Every Prisma read or write on a tenant-owned model filters by the tenant id from the session or request context, never from a client-supplied body field.
- No `findMany` without a tenant `where` on tenant-owned models.
- No raw SQL without a tenant predicate.
- Server actions and route handlers resolve the tenant before touching data.

### 2. Feature gates
- New routes and server actions that belong to a gated feature check the gate server-side, not only in the UI.
- Gated UI renders the upgrade prompt component, never a blank page or a 404.

### 3. Data layer
- If `schema.prisma` changed, a migration exists in `prisma/migrations` and `npx prisma validate` passes.
- New fields that back customer-facing data have sensible defaults or the migration backfills them.
- Deletions are soft where the data has billing or compliance value (memberships, calls, recruit communications).

### 4. AI calls
- Any new or changed LLM call goes through the router with a tier, budget, and fallback. If the diff touches one, run `/df-model-routing` and include its result.

### 5. UI states (any new screen or component)
- Loading, empty, and error states exist.
- Mobile layout checked for member-facing and recruit-facing screens.
- No hardcoded brand color, logo, or product name in Flex or LINDA screens.
- Copy in the diff passes the df-writing hard rules (no em-dashes, no AI tells).

### 6. Build health
- Run `npx tsc --noEmit`. Zero errors.
- Run `npm run lint` if present. Zero errors.
- Run `npm run build` only if `$ARGUMENTS` includes "full".

### 7. Secrets and config
- Nothing from the secrets grep above is a real credential. Env access goes through the existing env schema if the repo has one.

### 8. Tests
- If the repo has tests covering the touched area, run them. If it has none for a new billing, migration, or merge path, write the minimum test that exercises the tenant boundary.

## Output

Report as a short list, one line per checklist item, marked PASS, FAIL, or N/A. Under each FAIL give the file, line, and the exact change. If everything passes, propose a commit message in conventional commit format and, if `$ARGUMENTS` includes "commit", stage and commit directly.

Do not narrate the process. Findings only.
