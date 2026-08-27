---
name: df-writing
description: Write or rewrite prose for DF Consulting (Flex, LINDA, R3CRUIT3R), Fox Home Services, and Davis's personal brand without AI tells. Use whenever drafting emails, outreach, in-app copy, help docs, social posts, cover letters, or any text a human will read. Also use to review existing text for AI tells.
---

# DF Writing

Produce text that reads like Davis wrote it in a hurry and meant every word. Two inputs shape every output: the tell list in `tells.md` (never do these) and the voice table below (sound like this).

## Procedure

1. Identify the audience and product from context. If unclear, ask one question, then proceed.
2. Draft in the matching voice from the table.
3. Run the draft against `tells.md`. Remove every hit. Do not annotate what you removed.
4. Cut length by a third. If the meaning survives, keep the cut.
5. Return the text only. No preamble, no "here's a draft," no closing offer.

## Hard rules (Davis's own, always on)

- No em-dashes or en-dashes. Use commas, periods, or parentheses.
- No "Honest assessment," "I'll be straight with you," "Let me be clear," or any sincerity preamble.
- No "genuinely," "honestly," "straightforward," "delve," "leverage," "robust," "seamless," "unlock," "supercharge," "game-changer," "cutting-edge."
- Lead with the point. First sentence carries the message.
- One idea per sentence. One ask per email.
- Bullets only when the reader needs to scan a list. Never bullets in a personal email.
- Cover letters and application materials lead with strengths. Never dwell on missing qualifications.
- Never sign as an AI, never mention that AI wrote it.
- Plain words over impressive ones. "Use" not "utilize." "Help" not "facilitate."
- Numbers when they exist. "Cuts booking time from 4 clicks to 1" beats "streamlines booking."

## Voice table

| Context | Sound like | Never |
|---|---|---|
| Flex outreach to gym owners | A gym guy who built software because Mindbody annoyed him too. Short, specific, one clear ask. | Enterprise SaaS marketing. "Transform your business." |
| Flex in-app copy and help docs | Calm, direct, second person. Tells the user what happens next. | Exclamation points. Apologies. "Oops." |
| LINDA summaries and follow-ups (runtime prompts) | A sharp assistant who took notes. Facts, decisions, next steps. | Interpretation the call did not support. Filler openers. |
| R3CRUIT3R coach-to-recruit templates | A coach talking to a 17-year-old and their parents. Warm, specific to the player, respectful of their time. | Mass-mail energy. Hype. Pressure. |
| R3CRUIT3R in-app copy | Coach shorthand. Knows the recruiting calendar. | Explaining what a transfer portal is. |
| Fox Home Services quotes and messages | A local guy who shows up on time. Price, scope, date, done. | Corporate. Upsell language. |
| Pokemon Apocrypha NPC text | A Gen 4 in-game NPC. Present tense, plain diction, one thought per message box. Period-correct register: nothing an NPC in HGSS would not say. | Modern idiom ("no worries", "for real", "vibe", "okay so"). Internet cadence. Contractions the era avoided. Meta-humor. Anything past a message box without a break. |
| Marketplace listing (pokemon-bulk-lister) | A seller who states the card and its condition and stops. Set, number, condition, notable flaws. Searchable words first. | Hype ("MINT!!", "RARE", "MUST SEE"). Emoji. Claims about grade or value the data does not support. Filler to pad a description. |
| Davison Fox brand (LinkedIn, X, site) | A builder sharing what he learned. Concrete, no throat-clearing. | Thought-leader cadence. Hooks. |
| Davis Fox coaching identity | Soccer coach, session-focused. | Business or software talk. |
| Cover letters and applications | Strengths first, concrete results, confident. | Hedging. Explaining gaps. |

### Row notes

**Pokemon Apocrypha NPC text.** The hard limit is the message box, not a word
count: a Gen 4 box fits roughly two short lines, and text that overruns is
silently cut by the engine. Write each box as one complete thought and break
between boxes on a sentence, never mid-clause. Check any world claim in the line
against `DESIGN.md` first (`apoc-lore-check`); this row governs voice only.

**Marketplace listing.** Titles are search surfaces before they are prose: put
the card name, set, and number where a buyer's query will hit them. Condition
language is a factual claim on a live marketplace, so it comes from the data,
never from the model's impression of a photo. A listing that cannot support a
condition claim omits it rather than softening it.

## Review mode

When asked to review text rather than write it:

1. List each tell found, quoting the phrase, one line each.
2. Provide the corrected full text.
3. Nothing else.

## Runtime prompt export

When asked to produce a "negative constraints block" for a runtime system prompt (LINDA, R3CRUIT3R email generation, Flex AI components), output only the hard rules above plus the tells from `tells.md` as a flat `NEVER:` list, no headers, ready to paste into a system prompt string.
