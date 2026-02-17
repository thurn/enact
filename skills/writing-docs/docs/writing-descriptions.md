# Writing Effective Descriptions

Descriptions are the discovery mechanism for documentation. Agents read
descriptions to decide whether to load a document. A poor description makes
the doc effectively invisible regardless of its content quality.

## The Test

Every description must answer: **"Should I read this right now?"**

Two required components:

1. **What's inside**: Concrete subject matter, key terms, topics covered
2. **When to read it**: Triggering conditions, symptoms, situations

## Format for Document Index Entries

```markdown
**[doc-name.md](doc-name.md)**: [What it covers]. [When to read it].
```

### Good Examples

```markdown
**[progressive-disclosure.md](progressive-disclosure.md)**: Patterns for
splitting content across files so agents load only what's needed. Read when
organizing multi-file documentation or skills exceeding 500 lines.

**[billing-api.md](billing-api.md)**: Complete endpoint reference for the
billing API including authentication, webhooks, and error codes. Read when
implementing or debugging billing features.

**[migration-guide.md](migration-guide.md)**: Step-by-step database migration
procedure with rollback instructions. Read before running any schema migration.
```

### Bad Examples

```markdown
# Too vague - doesn't say what's inside
**[progressive-disclosure.md](progressive-disclosure.md)**: Documentation
patterns.

# No trigger - doesn't say when to read
**[billing-api.md](billing-api.md)**: Billing API reference.

# Restates the filename - adds zero information
**[migration-guide.md](migration-guide.md)**: Guide for migrations.
```

## Format for Skill `description` Fields

Skill descriptions have special rules. They're loaded into the system prompt at
startup and used to select which skill to activate from potentially 100+
available skills.

**Rules:**

- Write in third person (the description is injected into system prompts)
- State capabilities first, then "Use when..." for triggers
- Include specific key terms agents would search for
- **NEVER** summarize the skill's workflow or process

```yaml
# GOOD: Capabilities + triggering conditions
description: Extract text and tables from PDF files, fill forms, merge
  documents. Use when working with PDF files or when the user mentions PDFs,
  forms, or document extraction.

# GOOD: Clear triggers, no workflow summary
description: Use when creating documentation, writing skills, or organizing
  document hierarchies. Triggers on mentions of docs, skills authoring, or
  progressive disclosure.

# BAD: Summarizes workflow - agent may follow this instead of reading body
description: Use when writing docs - creates index, organizes hierarchy,
  writes descriptions, then validates all references.

# BAD: Too vague for selection from 100+ skills
description: Helps with documentation.
```

**Why no workflow in descriptions**: When a description summarizes workflow,
agents may follow the description as a shortcut instead of reading the full
skill body. This causes them to miss detailed instructions, nuances, and edge
cases covered in the body.

## Choosing Key Terms

Include words an agent would search for when facing the relevant problem:

- **Domain terms**: Technology names, API names, file types
- **Symptoms**: "orphaned", "undiscoverable", "missing references", "flaky"
- **Error messages**: Actual strings from logs or tools
- **Synonyms**: Cover common variations ("docs/documentation/reference",
  "index/catalog/registry")

Be specific enough for selection but broad enough for discovery. A description
mentioning "PDF, forms, document extraction" covers the main search vectors
for that skill.
