# /design briefs, ready to run

Paste any of these after `/design` (or run `/df-design <screen name>` and the skill will pick it up). Each asks for layout-distinct options, names the data, and states the constraints. Edit field names to match your Prisma schema.

---

## Flex

### Mindbody migration wizard
```
/design a Mindbody migration wizard for Flex. Route: /settings/import. User: gym owner on desktop, first week on Flex, nervous about losing data. One job: get clients, memberships, and class schedule from a Mindbody export into this tenant with zero surprises. Steps: upload export files, map columns to Flex fields (Client, Membership, ClassTemplate, Location), review conflicts (duplicates, unknown membership tiers, expired plans), dry-run summary with counts, confirm and import, progress with per-entity status. Show what will NOT import and why. Reuse the page header, form layout, and data table components. States: empty (no file yet), validating, conflicts found, importing, done, failed with retry. White-label safe, tenant colors only. Give me 4 options that differ in structure: full-page stepper, side-by-side mapping with live preview, checklist dashboard, and conversational one-question-at-a-time.
```

### Tenant branding settings
```
/design the tenant branding settings screen for Flex. Route: /settings/brand. User: gym owner on desktop. One job: make Flex look like their gym in under five minutes. Fields: logo upload (light and dark), primary and accent color, custom domain with DNS instructions and verification status, app name, member-facing welcome text. Live preview of the member login screen and the class booking screen updating as they edit. Reuse form layout and card components. States: unverified domain, verification pending, verified. 3 options: preview on the right, preview above with settings tabs below, and a full-screen preview with a floating settings drawer.
```

### Member class booking (mobile)
```
/design the member-facing class booking flow for Flex on mobile. Routes: /app/schedule and /app/schedule/[classId]. User: a gym member on their phone between meetings. One job: book or cancel a class in two taps. Data: ClassInstance (name, coach, start, capacity, spotsLeft), the member's Membership (credits remaining or unlimited), their existing bookings. Show a day strip, class cards with spots left, a booking sheet with confirm, and a waitlist state when full. Tenant colors only. States: no classes today, membership lapsed (show renew CTA, not an error), already booked, waitlisted. 3 options: vertical timeline, horizontal day strip with card stack, and a calendar month view with a bottom sheet.
```

### Owner dashboard
```
/design the gym owner dashboard for Flex. Route: /dashboard. User: owner on desktop, Monday morning, 90 seconds. One job: tell them what needs attention today. Cards: revenue MTD vs last month, active members and churn this month, trials expiring in 7 days, failed payments, today's classes and fill rate, LINDA call summary if the add-on is enabled (feature-gated card, show the upgrade prompt component when not). Reuse page header and card components. States: new tenant with no data (show a setup checklist instead of empty charts). 3 options: KPI row plus action list, action list first with KPIs collapsed, and a two-column "money left, people right" layout.
```

### Feature-gate upgrade prompt
```
/design the upgrade prompt component that renders when a Flex tenant hits a feature gate (for example the LINDA add-on or multi-location). Used inline in place of the gated content and as a modal. Content: what the feature does in one line, one screenshot or illustration slot, price, primary CTA "Add to plan", secondary "Learn more". Must work in any tenant's colors. 3 options: card, banner strip, and a blurred-preview overlay of the real feature behind the prompt.
```

---

## LINDA

### Call detail view
```
/design the call detail view for LINDA (a Flex add-on, must look like Flex). Route: /calls/[callId]. User: gym owner or front-desk manager on desktop reviewing a call after the fact. One job: understand the call in 20 seconds and act on it. Data: Call (caller, number, duration, timestamp, outcome, sentiment), Transcript (speaker-labeled segments with timestamps), Summary (2 to 3 lines), ActionItems (each with assignee and done state), Lead flag with confidence, audio player. Layout must let transcript and summary be visible together. Actions: mark as lead, create follow-up task, send follow-up email (draft generated), assign. States: transcript still processing, no audio, low-confidence classification flagged for review. 3 options: three-column (player and meta, transcript, summary and actions), two-column with a sticky summary header, and a single column with the transcript collapsed under the summary.
```

### Calls list
```
/design the calls list for LINDA. Route: /calls. User: owner scanning the day's calls on desktop or phone. Data: Call rows with time, caller, duration, outcome badge, lead flag, sentiment, assigned to, follow-up status. Filters: date range, outcome, lead only, unassigned. Bulk actions: assign, mark reviewed. Reuse the data table component. States: no calls yet (show the setup steps for connecting a phone number), filters return nothing. 3 options: dense table, card list grouped by day, and a split view with the list left and the selected call's summary right.
```

### LINDA entry point inside Flex
```
/design how LINDA appears inside the Flex navigation and dashboard when the add-on is enabled, and what a tenant sees when it is not. Include the nav item, a dashboard card with today's calls and open follow-ups, and the gated state using the upgrade prompt component. 3 options: LINDA as a top-level nav section, LINDA folded under a "Front desk" section, and LINDA as a dashboard-first experience with minimal nav presence.
```

---

## R3CRUIT3R

### Transfers table
```
/design the transfers table for R3CRUIT3R. Route: /transfers. User: assistant coach or recruiting coordinator on desktop, checking new portal entries each morning. Data: Transfer rows (player, previous school, position, class year, eligibility remaining, entered date, source, status: new / reviewed / matched / dismissed, match candidate count). Filters: position, entered in last N days, status, conference. Row action: open match review. Bulk: dismiss, mark reviewed. This is a separate table from Recruits by design; do not merge visually. Reuse the data table component. States: no new entries today, portal source disconnected. 3 options: dense table with a "new since yesterday" divider, kanban by status, and a feed view with match badges.
```

### Match and merge review
```
/design the match-and-merge review screen for R3CRUIT3R. Route: /transfers/[id]/match. User: coach on desktop making a careful decision. One job: decide whether this transfer is the same person as an existing recruit, and if so merge without losing data. Layout: transfer record on the left, up to three candidate recruits on the right with a confidence score and the fields that matched or conflicted highlighted. Field-by-field merge picker (keep left, keep right, keep both where the field allows). Actions: merge into selected, create new recruit, dismiss, skip. Confirmation showing what will change. States: no candidates (offer create new), one strong candidate (pre-select), conflicts requiring a choice. 4 options: side-by-side diff, stacked comparison with a sticky decision bar, wizard (pick candidate, then resolve fields, then confirm), and a single-card "is this the same player" yes/no flow that expands into field resolution only on yes.
```

### Recruit profile
```
/design the recruit profile for R3CRUIT3R. Route: /recruits/[id]. User: any coach on the staff, desktop or iPad. Data: Recruit (name, position, class year, school, club, contact, guardians, academics, eligibility), Notes timeline (who, when, what), Communications log (emails and calls with the df-writing style templates), Evaluations (rating, tags, film links), pipeline stage, assigned coach, transfer history if merged from a Transfer. Primary actions: log a note, send email, change stage. States: new recruit with sparse data, merged-from-transfer banner. 3 options: header plus tabbed sections, two-column with timeline right, and a single scrolling page with a sticky action bar.
```

### Coach pipeline board
```
/design the recruiting pipeline board for R3CRUIT3R. Route: /pipeline. User: head coach on desktop reviewing the whole board with staff in the room. Data: Recruits grouped by stage (identified, contacted, evaluating, offered, committed, lost), filters by position and class year, per-card position, class year, rating, assigned coach, days since last contact with a warning past 14 days. Drag between stages. States: empty stage, filter returns nothing. 3 options: horizontal kanban, table with stage as a column and inline stage change, and a position-by-stage matrix.
```

---

## Tactics board / session planner (personal app)

### Session planner
```
/design the session planner screen for my tactics board app. User: me, on a laptop the night before training, and on a phone at the field. One job: build a 75-minute session from drills in under five minutes and glance at it during practice. Data: Session (date, group, duration, focus), ordered Drill blocks (name, minutes, setup diagram thumbnail, coaching points, equipment), warm-up and cool-down slots. Actions: add drill from library, reorder, set minutes, duplicate last session, print or share. Phone view: one block at a time with a big timer. 3 options: timeline with drag handles, card grid, and a two-pane library-left session-right builder.
```

### Drill card
```
/design the drill card component for my tactics board app. Used in the library list, in the session planner, and full-screen on a phone at the field. Data: name, category, minutes, players needed, space, equipment, diagram, 3 coaching points, progressions. Full-screen mode is high contrast, large type, readable in sunlight. 3 options.
```
