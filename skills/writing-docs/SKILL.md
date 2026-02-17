---
name: writing-docs
description: Write and organize AI-friendly documentation, skills, and reference material. Use when creating docs, writing skills, building document indexes, structuring doc hierarchies, or applying progressive disclosure patterns. Triggers on documentation authoring, skills creation, docs organization, or index management.
---

# Writing AI-Friendly Documentation

Guide for writing documentation that AI agents can discover, navigate, and use
effectively. Covers project docs, skills, and reference material.

## The Golden Rule

**Every document must be referenced somewhere with a description.**

Unreferenced docs are invisible to agents. The basic pattern:

1. `docs/index.md` is the top-level document index
2. Index entries link to docs with descriptions stating what's inside and when
   to read it
3. Docs can link to child docs following the same description pattern
4. Every doc is reachable from the index (directly or through a parent)

**On creation**: Immediately add the new doc to its parent index with a
description. **On deletion**: Remove all references to it. No orphans, no
dangling links.

## Core Principles

**Conciseness**: The context window is shared. Only add context an agent doesn't
already have. Challenge each paragraph: "Does this justify its token cost?"
Claude already knows what PDFs are, how libraries work, and common programming
patterns.

**Progressive disclosure**: The overview file (SKILL.md or index.md) points to
detail files that agents load on demand. Unread files cost zero tokens. See
[docs/progressive-disclosure.md](docs/progressive-disclosure.md) for splitting
patterns.

**One level deep**: Link all reference files directly from the overview. Avoid
chains (A links to B links to C) because agents may partially read nested files,
missing content not near the top.

**Consistent terminology**: Pick one term per concept and use it everywhere.
Don't alternate between "endpoint/URL/route" or "field/element/control."

## Writing Descriptions

Every link needs a description answering: **"Should I read this right now?"**

Two required components:
- **What's inside**: Concrete subject matter, key terms
- **When to read**: Triggering conditions, situations, symptoms

```markdown
# Good: specific content + trigger
**[billing-api.md](billing-api.md)**: Complete endpoint reference for the
billing API including auth, webhooks, and error codes. Read when implementing
or debugging billing features.

# Bad: vague, no trigger
**[billing-api.md](billing-api.md)**: API reference.
```

For skill `description` fields and detailed guidance, see
[docs/writing-descriptions.md](docs/writing-descriptions.md).

## Document Hierarchy

```
project/
  docs/
    index.md          # Root index - links to all top-level docs
    architecture.md   # Linked from index.md with description
    api/
      index.md        # Sub-index, linked from docs/index.md
      auth.md         # Linked from api/index.md
      billing.md      # Linked from api/index.md
```

Rules: root index required, max two levels deep, sub-indexes when a topic has
3+ children. Full details in
[docs/document-hierarchy.md](docs/document-hierarchy.md).

## Quick Reference

| Rule | Detail |
|------|--------|
| Token budget | SKILL.md under 500 lines; split beyond that |
| References | One level deep from overview; no nested chains |
| Long files | TOC at top for files >100 lines |
| File naming | Descriptive (`form_validation.md`, not `doc2.md`) |
| Paths | Forward slashes only (`reference/guide.md`) |
| New docs | Immediately add to parent index with description |
| Deleted docs | Remove all references |
| Descriptions | Specific content + when to read; never vague |
| Terminology | One term per concept, consistent throughout |

## Writing Skills

Skills follow the same rules as general docs, plus:

- **YAML frontmatter**: `name` (64 chars, lowercase/numbers/hyphens) and
  `description` (1024 chars max)
- **Description**: Third-person. State capabilities, then "Use when..." with
  specific triggers. Never summarize workflow in the description.
- **Body**: Under 500 lines. Use progressive disclosure for larger content.
- **Supporting files**: Bundle reference material in separate files; agents load
  on demand.
- **Naming**: Gerund form preferred (`writing-docs`, `processing-pdfs`)
- **Degrees of freedom**: Match specificity to task fragility. Exact scripts for
  fragile operations; general guidance for judgment calls.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Orphaned docs | Add to parent index immediately on creation |
| Vague descriptions ("see docs") | State what's inside and when to read |
| Nested references (A->B->C) | Link all files from overview, one level deep |
| Verbose explanations | Remove what Claude already knows |
| Workflow in skill description | Description = when to use; body = how |
| Inconsistent terms | Pick one term per concept; grep to verify |
| No TOC on long files | Add TOC at top for files >100 lines |

## Document Index

All supporting docs for this skill. See
[docs/index.md](docs/index.md) for the full index.

- **[docs/progressive-disclosure.md](docs/progressive-disclosure.md)**: Patterns
  for splitting content across files so agents load only what's needed. Read
  when organizing multi-file documentation or skills exceeding ~300 lines.

- **[docs/writing-descriptions.md](docs/writing-descriptions.md)**: How to write
  descriptions for document links, index entries, and skill `description`
  fields. Read when writing index entries, skill metadata, or any reference
  that needs a discovery description.

- **[docs/document-hierarchy.md](docs/document-hierarchy.md)**: How to structure
  `docs/index.md` and nested document trees with enforcement rules. Read when
  setting up new documentation or reorganizing existing docs.
