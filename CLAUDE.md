# AGENTS.md

## Purpose
This file defines the required behavior for the coding agent. These rules are mandatory and apply to all code changes.

---

## 1. Read Before Write

Before making any changes, you MUST review:

- AGENTS.md
- SOLUTIONS.md (project root)
- /docs/OPENCLAW_HOW_IT_SHOULD_WORK.md — **read this first for all architecture, agent roles, exec rules, model tiers, cron patterns, and known failure modes**
- Relevant files in /docs
- Existing code patterns and adjacent modules

Do not begin implementation until the current system behavior and conventions are understood.

---

## 2. Bug Tracking and Memory

Whenever fixing a bug:

1. Clearly identify the problem
2. Determine and explain the root cause
3. Implement a correct fix (not a workaround)

You MUST record every bug fix in:

SOLUTIONS.md (project root)

### Required format:

[YYYY-MM-DD] - <Short Title>
Problem
<What was happening>
Root Cause
<Why it happened>
Solution
<What was changed and why it works>
Notes

<Edge cases, constraints, follow-ups>


Before fixing any issue:
- Check SOLUTIONS.md for related past bugs
- Ensure the same issue is not reintroduced

A bug fix is NOT complete unless SOLUTIONS.md is updated.

---

## 3. Code Quality and Comments

- Always include comments for:
  - Function purpose
  - Non-obvious logic
  - Assumptions and invariants

- Do NOT write redundant comments that restate the code

- Prioritize clarity and readability over cleverness

---

## 4. Contract and Data Structure Integrity

- Treat all interfaces, schemas, and data structures as stable contracts

- Do NOT:
  - Change return shapes
  - Rename fields
  - Alter function signatures
  - Modify file formats

unless absolutely necessary.

If a contract must change:
- Update all dependent code
- Update documentation in /docs
- Record the change in SOLUTIONS.md

---

## 5. Consistency and Reuse

- Reuse existing:
  - Functions
  - Utilities
  - Data structures
  - Naming conventions

- Do NOT introduce new patterns if an equivalent already exists

- Match the style and architecture of the existing codebase

---

## 6. Root Cause Discipline

- Fix root causes, not symptoms

- Avoid:
  - Superficial patches
  - Duplicate logic
  - Defensive clutter that hides deeper issues

---

## 7. Minimal, Local Changes

- Prefer the smallest possible change that fully resolves the issue

- Do NOT:
  - Refactor unrelated code
  - Rename broadly
  - Restructure modules without clear justification

---

## 8. Tests

If a test framework exists:

- For every bug fix:
  - Add or update a test when practical
  - Ensure the test would fail before the fix and pass after

- Do not skip tests when they are part of the project workflow

---

## 9. Documentation Requirements (/docs)

Documentation must remain consistent with the codebase.

You MUST update files under `/docs` when:

- A new feature is added
- Behavior changes
- Contracts or data structures change
- Configuration or usage changes

Do NOT leave documentation outdated.

---

## 10. Implementation Discipline

- Do not guess behavior

- Infer intended behavior from:
  - Existing code
  - Tests
  - Documentation
  - SOLUTIONS.md

- Maintain consistency with established patterns

---

## 11. Validation Before Commit

Before committing:

- Run relevant tests (if available)
- Ensure code compiles or runs
- Verify consistency across modified files

Do NOT commit broken or incomplete work unless explicitly required.

---

## 12. Automatic Commit Policy

After completing any coherent unit of work (bug fix, feature, refactor):

You MUST create a git commit.

### Conditions:
- Code is in a working state
- SOLUTIONS.md is updated (if applicable)
- /docs is updated (if applicable)
- Tests are passing (if present)

### Commit message format:

- fix: <short description>
- feat: <short description>
- refactor: <short description>
- docs: <short description>

### Examples:

- fix: resolve null pointer in ingestion pipeline
- feat: add batch document loader
- refactor: simplify vector store interface
- docs: update API usage for corpus explorer

---

## 13. Completion Requirements

A task is only complete when ALL of the following are true:

- Code changes are correct and minimal
- Contracts remain consistent (or fully updated)
- SOLUTIONS.md is updated (for bugs)
- /docs is updated (if behavior changed)
- Tests are added/updated when applicable
- A git commit has been created

---

## 14. Guiding Principle

Make the smallest correct change that:
- fixes the root cause,
- preserves system integrity,
- and keeps the codebase consistent, documented, and traceable.