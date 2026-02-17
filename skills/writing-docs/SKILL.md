---
name: writing-docs
description: Write and organize AI-friendly technical documentation and reference material. Use when creating docs, building document indexes, structuring doc hierarchies, or applying progressive disclosure patterns. Triggers on documentation authoring, docs organization, or index management.
---

# Writing AI-Friendly Documentation

Guide for writing technical documentation that AI agents can discover, navigate,
and use effectively.

## The Golden Rule

**Every document must be referenced somewhere with a description.**

Unreferenced docs are invisible to agents. The basic pattern:

1. `docs/index.md` is the top-level document index
2. Index entries reference docs with descriptions stating what's inside and when
   to read it
3. Docs can reference child docs following the same description pattern
4. Every doc is reachable from the index (directly or through a parent)

**On creation**: Immediately add the new doc to its parent with a description.
**On deletion**: Remove all references to it. No orphans, no dangling links.

## Core Principles

**Conciseness**: The context window is shared. Only add context an agent doesn't
already have. Challenge each paragraph: "Does this justify its token cost?"

**Progressive disclosure**: The overview file points to detail files that agents
load on demand. Unread files cost zero tokens. See
[progressive-disclosure.md](progressive-disclosure.md) for splitting patterns.

**One level deep**: Reference all files directly from the overview. Avoid chains
(A references B references C) because agents may partially read nested files,
missing content not near the top.

**Consistent terminology**: Pick one term per concept and use it everywhere.
Don't alternate between "endpoint/URL/route" or "field/element/control."

## Linking to Documents

Use **markdown links** `[file.md](path/to/file.md)` when referencing documents.
This is the format Anthropic recommends for progressive disclosure. Claude
recognizes markdown links as structured file references and reads the target on
demand when the content becomes relevant.

**In index entries** (description follows the link):

```markdown
- [architecture.md](architecture/architecture.md): System architecture overview
  covering service boundaries and data flow. Read when onboarding or planning
  cross-service features.
```

**In prose** (inline reference):

```markdown
For splitting patterns, see [progressive-disclosure.md](progressive-disclosure.md).
```

## Writing Descriptions

Every reference needs a description answering: **"Should I read this right now?"**

Two required components:
- **What's inside**: Concrete subject matter, key terms
- **When to read**: Triggering conditions, situations, symptoms

```markdown
# Good: specific content + trigger
- [billing-api.md](api/billing-api.md): Complete endpoint reference for the
  billing API including auth, webhooks, and error codes. Read when implementing
  or debugging billing features.

# Bad: vague, no trigger
- [billing-api.md](api/billing-api.md): API reference.
```

See [writing-descriptions.md](writing-descriptions.md) for detailed guidance.

## Document Hierarchy

No loose documents directly under `docs/` except `index.md`. Documents live in
topic directories, and multiple related docs can share a directory.

```
project/
  docs/
    index.md                  # Root index (only file at this level)
    architecture/
      architecture.md         # Topic directory (can hold one or many docs)
    api/
      api.md                  # Sub-index, referenced from index.md
      auth.md                 # Referenced from api.md
      billing.md              # Referenced from api.md
```

Rules: root index required, max two levels deep, sub-indexes when a topic has
3+ children. Full details in
[document-hierarchy.md](document-hierarchy.md).

## Quick Reference

| Rule | Detail |
|------|--------|
| Token budget | Main doc under 500 lines; split beyond that |
| References | Markdown links; one level deep; no nested chains |
| Directory rule | No loose files directly under `docs/`; group in topic dirs |
| Long files | TOC at top for files >100 lines |
| File naming | Descriptive (`form_validation.md`, not `doc2.md`) |
| Paths | Forward slashes only (`reference/guide.md`) |
| New docs | Immediately add to parent with description |
| Deleted docs | Remove all references |
| Descriptions | Specific content + when to read; never vague |
| Terminology | One term per concept, consistent throughout |
| Link format | `[file.md](path)` for refs; `@path` only in CLAUDE.md |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Orphaned docs | Add to parent immediately on creation |
| Loose files under `docs/` | Move into a topic directory |
| Vague descriptions ("see docs") | State what's inside and when to read |
| Nested references (A->B->C) | Reference all files from overview, one level deep |
| Verbose explanations | Remove what the agent already knows |
| Inconsistent terms | Pick one term per concept; grep to verify |
| No TOC on long files | Add TOC at top for files >100 lines |
| Using `@` in docs | Reserve for CLAUDE.md; use markdown links in docs |

## Supporting Documents

- [progressive-disclosure.md](progressive-disclosure.md): Patterns for splitting
  content across files so agents load only what's needed. Read when organizing
  multi-file documentation exceeding ~300 lines.

- [writing-descriptions.md](writing-descriptions.md): How to write descriptions
  for document references and index entries. Read when writing index entries or
  any reference that needs a discovery description.

- [document-hierarchy.md](document-hierarchy.md): How to structure `docs/index.md`
  and nested document trees with enforcement rules. Read when setting up new
  documentation or reorganizing existing docs.
