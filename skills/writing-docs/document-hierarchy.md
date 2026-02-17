# Document Hierarchy

How to structure documentation so every document is discoverable through a
connected hierarchy.

## The Golden Rule

**Every document must be referenced somewhere with a description.**

An unreferenced document is invisible to agents. It won't be discovered
regardless of its content quality.

## No Loose Files Under `docs/`

No loose documents directly under `docs/` except `index.md`. Documents live in
topic directories. Multiple related docs can share a directory — the rule is
about keeping `docs/` clean, not requiring a separate directory per document.

## Structure

```
project/
  docs/
    index.md                  # Root index (only file directly under docs/)
    architecture/
      architecture.md         # Topic directory (can hold one or many docs)
    api/
      api.md                  # Sub-index for API docs, referenced from index.md
      auth.md                 # Referenced from api.md
      billing.md              # Referenced from api.md
    guides/
      guides.md               # Sub-index for guides, referenced from index.md
      setup.md                # Referenced from guides.md
      deployment.md           # Referenced from guides.md
```

Sub-indexes use descriptive filenames matching their directory (e.g.,
`api/api.md`, `guides/guides.md`), not `index.md`.

## Index Format

Each index references its children using markdown links with descriptions,
following the rules in [writing-descriptions.md](writing-descriptions.md).

```markdown
# Project Documentation

## Architecture

- [architecture.md](architecture/architecture.md): System architecture overview
  covering service boundaries, data flow, and deployment topology. Read when
  onboarding, planning cross-service features, or debugging inter-service issues.

## API Reference

- [api.md](api/api.md): Sub-index for all API documentation covering auth,
  billing, and user endpoints. Read when working on any API task.

## Guides

- [guides.md](guides/guides.md): Sub-index for operational guides including
  setup, deployment, and troubleshooting. Read when performing ops tasks or
  onboarding.
```

## Rules

1. **Root index required**: Every `docs/` directory has an `index.md`
2. **No loose files**: No loose files under `docs/` except `index.md`
3. **Sub-indexes use descriptive names**: `api/api.md` not `api/index.md`
4. **Every doc referenced**: Each document appears in exactly one parent with a
   description
5. **Sub-indexes for groups**: Create a sub-index when a topic has 3+ child docs
6. **Max two levels**: `docs/index.md` -> `topic/topic.md` -> `topic/detail.md`
   — no deeper
7. **Create reference on creation**: When you write a new doc, immediately add
   it to the parent
8. **Remove reference on deletion**: When you delete a doc, remove it from all
   parents

## Enforcement Checklist

After any documentation change:

- [ ] New docs have an entry in their parent with a description
- [ ] New docs are in a topic directory (not loose under `docs/`)
- [ ] Deleted docs have been removed from all parents
- [ ] Each entry describes what's inside and when to read it
- [ ] No document is more than two levels from the root index
- [ ] No dangling references (pointing to deleted or moved docs)
