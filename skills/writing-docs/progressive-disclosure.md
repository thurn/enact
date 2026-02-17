# Progressive Disclosure Patterns

How to split documentation across files so agents load only what they need.
Unread files cost zero context tokens.

## When to Split

- Main doc exceeds ~300 lines
- Content has distinct domains (e.g., finance vs. sales schemas)
- Some sections apply only to specific tasks
- Reference material is large (API docs, schema definitions)

## Pattern 1: High-Level Guide with References

The main doc provides overview and quick start. Separate files hold detailed
reference material.

```
project/
  docs/
    index.md              # Overview + quick start + references to detail
    reference.md          # Detailed API reference
    examples.md           # Usage examples
    advanced.md           # Advanced features
```

In the main doc, reference each file with a description:

```markdown
- reference.md: Complete method signatures and parameters for the data
  processing API.
- examples.md: Common usage patterns with runnable code.
- advanced.md: Caching, batching, and webhook configuration.
```

Agents load reference.md, examples.md, or advanced.md only when needed.

## Pattern 2: Domain-Specific Organization

When content spans multiple domains, organize by domain so agents load only the
relevant one.

```
project/
  docs/
    index.md
    reference/
      finance.md          # Revenue, billing metrics
      sales.md            # Pipeline, opportunities
      product.md          # Usage analytics
```

The index references each domain file with a description of its contents. When a
user asks about revenue, the agent reads only `reference/finance.md`.

## Pattern 3: Conditional Details

Show basic content inline. Reference advanced content for specific scenarios.

```markdown
## Creating documents

Use docx-js for new documents. Basic usage:
[inline code example]

For tracked changes (redline markup rules and OOXML change-tracking format),
see redlining.md.

For OOXML internals (raw XML structure and namespace reference), see ooxml.md.
```

Agents read redlining.md or ooxml.md only when the user needs those features.

## Rules

- **One level deep**: All files referenced directly from the overview. Never
  chain overview -> intermediate -> actual content.
- **Table of contents**: Files over 100 lines get a TOC at the top so agents
  can see scope even with partial reads.
- **Descriptive file names**: `form_validation.md` not `doc2.md`.
- **Forward slashes only**: Always `reference/guide.md`, never
  `reference\guide.md`.

## Anti-Pattern: Nested References

```markdown
# BAD: Content buried three levels deep
index.md -> advanced.md -> details.md -> actual-content.md

# GOOD: All files one level from overview
index.md -> advanced.md    (contains the detail directly)
index.md -> details.md     (also referenced from overview)
```

Agents may preview nested files with partial reads (e.g., first 100 lines),
missing content that isn't near the top. Keep everything one hop away.
