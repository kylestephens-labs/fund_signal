# Refactorer Codex Rules and Success Criteria

- Preserve existing functionality and keep all current tests passing.
- Reduce complexity and redundancy; prefer clear, intention-revealing structure.
- Improve naming and file organization for readability.
- Follow linting/formatting standards already enforced in the repo.
- Increase modularity/testability when possible without over-engineering.
- Adhere to project conventions and security best practices.
- Keep changes minimal: avoid new abstractions unless they clearly reduce duplication or risk.

## Operating Guidelines

1. **Scope** – Touch only the files explicitly listed in each task prompt. Never revert user changes you didn’t author.
2. **Context First** – Read the referenced docs (e.g., `docs/mvp.md`, `docs/data_pipelines.md/*`) before editing so refactors stay aligned with product intent.
3. **Testing** – Run targeted tests whenever feasible. If tooling isn’t available, document the exact command you attempted.
4. **Determinism** – Many pipelines rely on byte-for-byte reproducibility. Preserve ordering, timestamps, and hashing semantics unless the task explicitly changes them.
5. **Comments & Docs** – Keep comments concise and only where they add real clarity. Update relevant docs or READMEs whenever behavior/context is clarified.
6. **Final Response Format** – Summaries must follow the builder hand-off template (see “Summary Expectations” below) so future prompts can embed them verbatim.

## Summary Expectations

Conclude every refactorer codex task with a summary of the following sections

- `IMPLEMENTATION SUMMARY` (bullet list of concrete improvements)
- `Files created`
- `Files modified`
- `Test coverage`
- `Commits`
- `Needs Improvement / Follow-ups`
- `Optimization Ideas`

Keep wording concise so the output can be dropped directly into future next prompts as needed.

