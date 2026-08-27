---
name: df-model-routing
description: Audit every runtime LLM call in a DF Consulting repo (Flex, LINDA, R3CRUIT3R) against the cost-tier routing policy. Use when adding or changing any Anthropic API call, when the user says "route check" or "cheaper tier?", before merging AI features, or on a monthly routing review. Flags Fable at runtime, wrong tiers, hardcoded model strings, and missing fallbacks.
allowed-tools: Read Grep Glob
---

# DF model routing audit

## Policy (source of truth)

Runtime calls use only Claude Haiku, Sonnet, or Opus, current versions preferred per task difficulty.

| Tier | Use for | Examples |
|---|---|---|
| Haiku | High volume, low judgment, structured output | Classification, extraction, tagging, PII redaction, routing decisions, simple summaries under 200 words |
| Sonnet | Mid-tier reasoning, customer-facing prose, multi-step tool use | LINDA call summaries and follow-up drafts, R3CRUIT3R recruit emails, Flex onboarding copy, match scoring explanations |
| Opus | Quality-critical, judgment-heavy, low volume | Migration conflict resolution recommendations, ambiguous merge decisions escalated from Sonnet, anything a customer will make a money decision on |
| Local (when present) | Tier zero: PII redaction, first-pass labels, bulk transforms | Never customer-facing text |

Hard rules:

- Fable 5 (any `claude-fable*` string) is never a runtime dependency. It is a build-time tool in Claude Code only.
- No model string is hardcoded at a call site. All calls go through the router (`lib/ai/router.*` or equivalent) which resolves a tier name to a model id from one config file.
- Every call declares a tier, a max token budget, and a fallback tier.
- High-volume paths (anything triggered per call, per message, per row, or per webhook) never default to Opus.
- Runtime prompts that produce customer-facing text include the df-writing negative-constraints block.

## Current call sites

Model strings and SDK usage found in this repo:

!`grep -rn --include=*.ts --include=*.tsx --include=*.js --include=*.mjs --include=*.py -E "claude-(fable|opus|sonnet|haiku)[a-z0-9.-]*|anthropic\.messages\.create|messages\.create\(|@anthropic-ai/sdk|model:\s*['\"]claude" . 2>/dev/null | grep -v node_modules | grep -v "\.next/" | grep -v "\.claude/" | head -80`

Router and config files:

!`find . -path ./node_modules -prune -o -path ./.next -prune -o \( -iname "*router*" -o -iname "*models*" -o -iname "*llm*" -o -iname "*ai-config*" \) -print 2>/dev/null | grep -Ei "\.(ts|tsx|js|json)$" | head -30`

## Procedure

1. Read every file listed above. For each call site record: file and line, what the call does, the tier or model used, estimated volume (per request, per record, per day, on demand), whether it produces customer-facing text, whether a fallback exists.
2. Check each against the policy. Classify findings as:
   - **Block**: Fable at runtime, Opus on a high-volume path, hardcoded model string outside the router config.
   - **Fix**: missing fallback, missing token budget, customer-facing text without the writing constraints block, tier higher than the task needs.
   - **Consider**: a Sonnet task that could be Haiku with a stricter schema, a Haiku task that could move to local.
3. Output a table: call site, purpose, current tier, recommended tier, finding level, one-line fix.
4. If asked to fix (or if `$ARGUMENTS` contains "fix"), apply the changes directly in the repo: move hardcoded strings into the router config, set tiers, add fallbacks. Commit with the message `chore(ai): routing audit fixes`. Do not add a local-only patch file.
5. Finish with a two-line summary: number of call sites, number of blocks remaining.

## Notes

- If no router exists, propose one file: a `tiers` map (haiku, sonnet, opus, local) resolving to current model ids, a `call(tier, input, options)` wrapper with fallback, and a per-tier default max token budget. Keep it under 80 lines.
- Volume estimates come from the trigger, not from guesses about traffic. A webhook handler is high volume by definition.
