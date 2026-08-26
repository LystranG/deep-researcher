# Domain Docs

How engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- `CONTEXT.md` at the repo root.
- `docs/adr/` decisions that touch the area being investigated.

If any of these files do not exist, proceed silently. Do not flag their absence or suggest creating them upfront. The domain-modeling skill creates them lazily when terms or decisions are actually resolved.

## File structure

This is a single-context repository:

```text
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-domain-state-and-langgraph-runtime.md
│   └── ...
└── src/
```

## Use the glossary's vocabulary

When output names a domain concept, such as in an issue title, refactor proposal, hypothesis, or test name, use the term as defined in `CONTEXT.md`. Do not drift to synonyms the glossary explicitly avoids.

If the concept you need is not in the glossary yet, either reconsider the terminology or note the gap for the domain-modeling skill.

## Flag ADR conflicts

If output contradicts an existing ADR, surface it explicitly rather than silently overriding the decision:

> _Contradicts ADR-0007 — but worth reopening because…_
