# Preserve the approved application design

The user approved the ivory/forest redesign on 16 November 2026, then explicitly
asked to restore it and prevent Claude from undoing it. Keep that design unless
the user requests a new visual direction. Functional fixes are welcome; preserve
the shared theme, navigation, responsive layouts, and accessible disclosures in
`pokemon-bulk-lister/webapp/static/design.css` and
`pokemon-bulk-lister/webapp/templates/_navigation.html`. See `CLAUDE.md` for the
design details. Retain the stable Scan photos and automatic saving of confirmed,
priced cards to the library.

## Production releases

The shared integration/release branch is `claude/dreamy-archimedes-7c38oi`.
Fetch and merge its latest changes before working or releasing. Never publish an
older worktree over production: it previously removed the approved design.
Merge reviewed changes into this shared branch, preserving other contributors'
functional fixes. `main` is not currently the source of production releases.

After tests pass and the release is committed and pushed, run:

```sh
python pokemon-bulk-lister/scripts/prepare_release.py
```

This refuses a dirty tracked tree, a commit missing approved design `1ca010c`, or
a HEAD different from the freshly fetched release branch. It prints a clean
staging directory containing only committed files. Deploy from that staging
repository root with one `vercel deploy --prod` build; do not deploy the working
checkout or upload local configuration, photos, or databases. Record the release
commit in deployment metadata and verify `https://bulk.dfconsulting.tech` plus its
static assets after deployment. Use the existing Vercel project and environment.
