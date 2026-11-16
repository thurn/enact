# Cirrus and the Enact dashboard

Every dashboard change serves two priorities, in this order.

1. **UI performance.** The dashboard should feel instant. Cache rendered
   HTML by ticket version, and use a content hash and a one-year browser cache
   for CSS and JavaScript. Show cached content during revalidation. Prefetch
   links on hover, focus and touch, and cards as they scroll into view. Use the
   ticket feed to receive changes and swap in updated content without moving
   the reader. Keep expensive fragments off the critical path and use
   placeholders that reserve their space. Nothing on a
   page's path calls `bd`. Measure before and after a change. Compare cold
   and warm rendering, and keep prefetched navigation to a few milliseconds.
2. **Build every page from Cirrus components.** Add a component here when a
   page needs a new pattern. Use the shared spacing, type, colour and motion
   tokens. Use system fonts and reserve colour for meaning.

## Design principles

1. **Simplicity.** Show what matters now. Each visible element helps the
   reader understand, decide or act. Remove borders, labels, icons, controls
   and decoration when clarity and discoverability allow.
2. **Progressive disclosure.** Keep primary actions immediately available
   and put secondary information one interaction away. Use hovercards,
   tooltips, popovers and disclosures. Support click, touch and keyboard
   focus as well as hover.
3. **Whitespace and typography.** Use space to group related items and
   separate distinct ones. Use the type scale, weight and contrast to convey
   hierarchy. The shared scales live in `static/css/1-tokens.css`.
4. **Visual hierarchy.** Give each page a focal point, clear primary actions
   and a natural reading order. Use alignment, proximity, contrast and
   scale to keep important information prominent.
5. **Precision and restraint.** Use consistent alignment, considered
   proportions, subtle colour and precise spacing throughout.
6. **Purposeful motion.** Show where elements come from, where they go and
   how states relate. Keep layout stable while content loads. Use restrained
   durations and natural easing, respect `prefers-reduced-motion`, and keep
   interactions available during animation.

## Files and checks

`components.ts` renders HTML from text and fragments. Escape all text, and
pass trusted HTML only to arguments intended for fragments. `markdown.ts`
renders the subset used in ticket descriptions and comments.

Wrangler builds the files in `server/static/` into static assets. The page
component's `root` sets the project home and navigation base. Use
`assetRoot: "/"` for pages under a project prefix, since the fingerprinted
files are served at the Worker origin. The service worker keeps the fixed
URL `/sw.js`.

Run `npm test` in `server/` for the HTML parity tests and the local Worker
asset tests. For changes that affect rendering, check a page in the browser
at desktop and 375 px widths in light and dark modes.
