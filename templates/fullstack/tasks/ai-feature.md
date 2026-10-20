---
title: AI feature behind a provider interface
status: todo
owner:
deps: [api-contract, repo-scaffold]
touches: [backend/ai]
prio: 1
---
## Goal
The contest rewards good AI use. Wrap the LLM call in one small module with a mock implementation.

## Acceptance criteria
- [ ] backend/ai module with `real` and `mock` providers selected by env var; key from env
- [ ] Prompts stored in one file, versioned; structured output validated; timeout and error handling with graceful fallback
- [ ] Endpoint(s) from the contract implemented; tests run against the mock

## Notes
