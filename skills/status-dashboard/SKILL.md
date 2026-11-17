---
name: status-dashboard
description: Work on Enact's live ticket dashboard and its cirrus design system under server/src/dashboard, server/src/cirrus, and server/static.
---

# Working on the ticket dashboard

Read Enact's root `AGENTS.md`. The Worker serves the dashboard from
`server/src/dashboard/`. Shared components live in `server/src/cirrus/`,
and their browser assets live in `server/static/`.

Run the server's configured tests with `npm --prefix server test`. Use an
isolated local Worker and inspect the changed pages in a headless browser at
desktop and 375 px widths, in both colour schemes. Pick a free local port
and keep fixture data isolated from production.

Measure cold and warm page rendering before and after the change. Preserve
cached navigation, stable content during refresh, and the live change feed.
Record the measurements with the change.

Build from cirrus's components and tokens. Add shared UI behavior there
when a component needs it. Apply its six principles.

1. Keep elements that help the reader understand, decide, or act.
2. Reveal secondary information in context with keyboard and touch access.
3. Use spacing and typography to establish structure.
4. Keep primary information prominent and secondary details quiet.
5. Preserve consistent alignment, proportions, colour, and motion.
6. Use motion to explain state changes and honour reduced-motion settings.
