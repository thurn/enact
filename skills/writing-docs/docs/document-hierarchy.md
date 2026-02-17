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
    index.md              # Root index - links to all top-level docs
    architecture.md       # Linked from index.md
    api/
      index.md            # Sub-index, linked from docs/index.md
      auth.md             # Linked from api/index.md
      billing.md          # Linked from api/index.md
    guides/
      index.md            # Sub-index, linked from docs/index.md
      setup.md            # Linked from guides/index.md
      deployment.md       # Linked from guides/index.md
```

## Index Format

Each index file lists its children with descriptions following the rules in
[writing-descriptions.md](writing-descriptions.md).

```markdown
# Project Documentation Index

## Architecture

**[architecture.md](architecture.md)**: System architecture overview covering
service boundaries, data flow, and deployment topology. Read when onboarding,
planning cross-service features, or debugging inter-service issues.

## API Reference

**[api/index.md](api/index.md)**: Sub-index for all API documentation covering
auth, billing, and user endpoints. Read when working on any API task.

## Guides

**[guides/index.md](guides/index.md)**: Sub-index for operational guides
including setup, deployment, and troubleshooting. Read when performing ops
tasks or onboarding.
```

## Rules

1. **Root index required**: Every `docs/` directory has an `index.md`
2. **Every doc referenced**: Each document appears in exactly one parent index
   with a description
3. **Sub-indexes for groups**: Create a sub-index when a topic has 3+ child
   docs
4. **Max two levels**: `docs/index.md` -> `topic/index.md` -> `topic/detail.md`
   — no deeper
5. **Create reference on creation**: When you write a new doc, immediately add
   it to the parent index
6. **Remove reference on deletion**: When you delete a doc, remove it from all
   indexes

## Enforcement Checklist

After any documentation change:

- [ ] New docs have an entry in their parent index with a description
- [ ] Deleted docs have been removed from all indexes
- [ ] Each entry describes what's inside and when to read it
- [ ] No document is more than two levels from the root index
- [ ] No dangling links (references to deleted or moved docs)

## Skills as Documentation

Skills follow the same hierarchy. SKILL.md acts as the overview/index:

```
skill-name/
  SKILL.md                # Overview - links to supporting files with
                          # descriptions (acts as index)
  docs/
    index.md              # Consolidated document index
    reference.md          # Linked from SKILL.md and docs/index.md
    patterns.md           # Linked from SKILL.md and docs/index.md
```

SKILL.md links supporting files with descriptions (like an index). The
`docs/index.md` provides a consolidated view of all supporting material,
also with descriptions.
