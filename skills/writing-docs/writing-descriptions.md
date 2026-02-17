# Writing Effective Descriptions

Descriptions are the discovery mechanism for documentation. Agents read
descriptions to decide whether to load a document. A poor description makes
the doc effectively invisible regardless of its content quality.

## The Test

Every description must answer: **"Should I read this right now?"**

Two required components:

1. **What's inside**: Concrete subject matter, key terms, topics covered
2. **When to read it**: Triggering conditions, symptoms, situations

## Format

```markdown
- doc-name.md: [What it covers]. [When to read it].
```

### Good Examples

```markdown
- progressive-disclosure.md: Patterns for splitting content across files so
  agents load only what's needed. Read when organizing multi-file documentation
  exceeding 500 lines.

- billing-api.md: Complete endpoint reference for the billing API including
  authentication, webhooks, and error codes. Read when implementing or debugging
  billing features.

- migration-guide.md: Step-by-step database migration procedure with rollback
  instructions. Read before running any schema migration.
```

### Bad Examples

```markdown
# Too vague - doesn't say what's inside
- progressive-disclosure.md: Documentation patterns.

# No trigger - doesn't say when to read
- billing-api.md: Billing API reference.

# Restates the filename - adds zero information
- migration-guide.md: Guide for migrations.
```

## Choosing Key Terms

Include words an agent would search for when facing the relevant problem:

- **Domain terms**: Technology names, API names, file types
- **Symptoms**: "orphaned", "undiscoverable", "missing references", "flaky"
- **Error messages**: Actual strings from logs or tools
- **Synonyms**: Cover common variations ("docs/documentation/reference",
  "index/catalog/registry")

Be specific enough for selection but broad enough for discovery. A description
mentioning "billing API, authentication, webhooks, error codes" covers the main
search vectors for that doc.
