# Document Hierarchy

How to structure documentation so every document is discoverable through a
connected hierarchy.

## The Golden Rule

**Every document must be referenced somewhere with a description.**

An unreferenced document is invisible to agents. It won't be discovered
regardless of its content quality.

## Structure

```
project/
  docs/
    index.md              # Root index - references all top-level docs
    architecture.md       # Referenced from index.md
    api/
      api.md              # Sub-index for API docs, referenced from index.md
      auth.md             # Referenced from api.md
      billing.md          # Referenced from api.md
    guides/
      guides.md           # Sub-index for guides, referenced from index.md
      setup.md            # Referenced from guides.md
      deployment.md       # Referenced from guides.md
```

Sub-indexes use descriptive filenames matching their directory (e.g.,
`api/api.md`, `guides/guides.md`), not `index.md`.

## Index Format

Each index references its children with descriptions following the rules in
writing-descriptions.md.

```markdown
# Project Documentation

## Architecture

- architecture.md: System architecture overview covering service boundaries,
  data flow, and deployment topology. Read when onboarding, planning
  cross-service features, or debugging inter-service issues.

## API Reference

- api/api.md: Sub-index for all API documentation covering auth, billing, and
  user endpoints. Read when working on any API task.

## Guides

- guides/guides.md: Sub-index for operational guides including setup,
  deployment, and troubleshooting. Read when performing ops tasks or onboarding.
```

## Rules

1. **Root index required**: Every `docs/` directory has an `index.md`
2. **Sub-indexes use descriptive names**: `api/api.md` not `api/index.md`
3. **Every doc referenced**: Each document appears in exactly one parent with a
   description
4. **Sub-indexes for groups**: Create a sub-index when a topic has 3+ child docs
5. **Max two levels**: `docs/index.md` -> `topic/topic.md` -> `topic/detail.md`
   — no deeper
6. **Create reference on creation**: When you write a new doc, immediately add
   it to the parent
7. **Remove reference on deletion**: When you delete a doc, remove it from all
   parents

## Enforcement Checklist

After any documentation change:

- [ ] New docs have an entry in their parent with a description
- [ ] Deleted docs have been removed from all parents
- [ ] Each entry describes what's inside and when to read it
- [ ] No document is more than two levels from the root index
- [ ] No dangling references (pointing to deleted or moved docs)
