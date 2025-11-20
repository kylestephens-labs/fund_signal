# MCP v1 Plan — Tooling for Codex Roles

## Goal Recap
- Give Builder/Refactorer Codex structured repo access (read, search, summarize) and self-testing via prove targets so they stop depending on pasted context or manual test runs.
- Keep scope tight: no writes, no git, just context + prove bridges that align with the existing make/prove workflow.

## Key Outcomes
- **Self-service tests**: Builder/Refactorer call `prove.run` (make prove-quick/full) and act on failures without user intervention.
+- **Scoped context**: Codex fetches candidate files and summaries instead of whole-file dumps, avoiding token bloat and guessing.
- **Safety by restriction**: Read-only tools only; editing stays with the IDE/agent workflow.
- **Drop-in integration**: Minimal server config and package deps; ready for MCP-aware clients.

## Proposed Implementation Improvements

### 1. File Tree (v1)
```
.project/mcp/
  server.config.yaml
  package.json
  tools/
    fs.read.ts
    context.select.ts
    context.summarize.ts
    prove.run.ts
```
Scope: four tools only; no fs.write or git.exec in v1.

### 2. Tools (80/20 implementations)
- `fs.read.ts`: UTF-8 file read, returning `{ path, data }`. Keep it simple and rely on caller for line slicing.
- `context.select.ts`: globby-based path scorer using task terms; defaults to `app/**/*.py`, `tests/**/*.py`, ignores `.venv/.git/__pycache__`, and limits results (e.g., 24). Later: semantic/token scoring if needed.
- `context.summarize.ts`: head summarizer with max lines (default 200) returning `{ path, summary, totalLines }`. Head-only keeps token use predictable; add tail-on-request later.
- `prove.run.ts`: bridges to `make prove-quick` / `make prove-full` via `execa`, returning `{ mode, command, exitCode, ok, stdout, stderr }`. This hides pytest/ruff details from Codex and aligns with the Makefile gates.

### 3. Minimal server.config.yaml
```
server:
  name: fund-signal-mcp
  transport: stdio
  workingDir: .

limits:
  maxExecMs: 300000
  maxOutputKb: 4096

tools:
  - id: fs.read
    path: .project/mcp/tools/fs.read.ts
  - id: context.select
    path: .project/mcp/tools/context.select.ts
  - id: context.summarize
    path: .project/mcp/tools/context.summarize.ts
  - id: prove.run
    path: .project/mcp/tools/prove.run.ts
```

### 4. Package & Deps
`.project/mcp/package.json`
```json
{
  "name": "fund-signal-mcp",
  "version": "0.1.0",
  "type": "module",
  "private": true,
  "dependencies": {
    "execa": "^9.0.0",
    "globby": "^14.0.0"
  },
  "devDependencies": {
    "typescript": "^5.0.0",
    "ts-node": "^10.9.2"
  }
}
```
Install with `npm install` from `.project/mcp`.

### 5. Role Alignment (Builder/Refactorer)
- Builder: use `context.select` → `context.summarize`/`fs.read` for context; run `prove.run` with `mode: "quick"` before handoff; report command + result; don’t ask user to paste files.
- Refactorer: same context tools within Builder’s footprint; run `prove.run` with `mode: "quick"` after changes; report command + result; escalate if failures imply scope expansion.
Task Writer can stay unchanged for v1; it already lists prove-quick/full commands.

### 6. Smoke Tests (after wiring)
- `npx ts-node tools/prove.run.ts` (or small harness) should return structured JSON from `make prove-quick`.
- `npx ts-node tools/context.select.ts` with a sample task should list scored paths; `fs.read` should fetch one file; `context.summarize` should respect `maxLines`.

## Next Steps
1. Add the MCP file tree, tools, package.json, and server.config.yaml.
2. Install deps (`npm install` in `.project/mcp`) and smoke-test `prove.run`, `context.select`, and `fs.read`.
3. Update Builder/Refactorer role files with an MCP usage note (prefer MCP tools for context and prove).
4. Wire MCP into your Codex client (point at `.project/mcp/server.config.yaml`) and validate the tool catalog loads.

## Tasks

### Task 1: Scaffold MCP v1 files
Create the `.project/mcp` tree with server config, package.json, and the four tool stubs. Install deps and verify the MCP server can load the catalog. 📚 ESSENTIAL CONTEXT:
  • docs/mcp_v1/mcp_implementation_final.md
  • .project/mcp/server.config.yaml
  • .project/mcp/package.json

### Task 2: Implement tools + smoke tests
Fill in `fs.read.ts`, `context.select.ts`, `context.summarize.ts`, and `prove.run.ts` per the specs. Add a tiny TS harness to call `prove.run` (quick) and `context.select`/`fs.read` for local verification. 📚 ESSENTIAL CONTEXT:
  • docs/mcp_v1/mcp_implementation_final.md
  • .project/mcp/tools/*.ts
  • Makefile (prove targets)

### Task 3: Update Codex role prompts
Add an “MCP Tool Usage” note to Builder/Refactorer roles: use context tools for discovery/reads and `prove.run("quick")` for self-tests; fall back to make commands if tools unavailable. 📚 ESSENTIAL CONTEXT:
  • .project/codex/roles/builder.md
  • .project/codex/roles/refactorer.md
  • docs/mcp_v1/mcp_implementation_final.md
