Love this—this is the right time to treat MCP like a product with versions instead of “a pile of tools”.

I’ll give you:
	•	A versioned roadmap (v1 → v4+)
	•	For each version:
	•	Goal
	•	Capabilities / tools
	•	How it changes your current tri-Codex + Prove workflow
	•	Concrete repo changes (at a high level)

⸻

🧩 Overview: MCP Versions for Your System

Version	Theme	Who benefits most
v1	Context + Prove tools	Builder, Refactorer
v2	Editing + Git + Telemetry	Builder, Refactorer, You (audits)
v3	Search/Index + “Context Stack v2”	All Codex roles, Task Writer, Architect
v4+	Orchestrators + Repo-as-API	Future autonomous agents, CI, bots

You’re asking to “expand and make MCP v1” AND see what v2–v4 look like. Let’s start with v1 (concrete), then sketch the higher versions so you know what you’re building toward.

⸻

🧱 MCP v1 — Context + Prove Tools

🎯 Goal

Give Builder and Refactorer Codex:
	•	A reliable way to find & summarize relevant code without you pasting files
	•	A clean way to read specific files
	•	A standardized way to run Prove gates (make prove-quick / full)

v1 = Better context + self-testing, no repo writes, no git, no orchestration.

⸻

🛠 Capabilities / Tools in v1

Directory:

.project/
  mcp/
    server.config.yaml
    package.json
    tools/
      fs.read.ts
      context.select.ts
      context.summarize.ts
      prove.run.ts

Tools:
	1.	fs.read
	•	Input: { path }
	•	Output: { path, data }
	•	Purpose: Let Codex inspect file contents safely and deterministically.
	2.	context.select
	•	Input: { task, include?, exclude?, limit? }
	•	Output: { files: string[] }
	•	Purpose: Given the Task Template text, return the top N likely-relevant files (filenames only, 80/20 heuristic).
	3.	context.summarize
	•	Input: { path, maxLines? }
	•	Output: { path, summary, totalLines }
	•	Purpose: Return first N lines of a file so Codex gets enough structure without blowing tokens.
	4.	prove.run
	•	Input: { mode: "quick" | "full" }
	•	Output: { mode, command, exitCode, ok, stdout, stderr }
	•	Purpose: Let Builder/Refactorer run make prove-quick / make prove-full and see structured results.

⸻

🧠 How MCP v1 changes your tri-Codex workflow

Before v1:
	•	You copy/paste context into Codex.
	•	Codex “guesses” relevance or relies on open files.
	•	You run make prove-quick and interpret output.
	•	You tell Codex what failed.

After v1:
	•	Builder Codex:
	•	Calls context.select → gets candidate files
	•	Calls context.summarize/fs.read → builds its own context
	•	Calls prove.run("quick") → sees errors, fixes code, reruns until ok: true
	•	Refactorer Codex:
	•	Uses fs.read to inspect Builder’s footprint
	•	Calls prove.run("quick") after refactors to ensure behavior preserved

Your role:
	•	Less “context shuttle”, less “test runner”, more reviewer/architect.

⸻

📂 Concrete repo changes for v1
	1.	Add MCP files

.project/
  mcp/
    server.config.yaml
    package.json
    tools/
      fs.read.ts
      context.select.ts
      context.summarize.ts
      prove.run.ts

	2.	Wire tools as described in your existing spec (the code you pasted is already good).
	3.	Update roles to mention MCP:

	•	Builder:
	•	Use context.select + context.summarize/fs.read for context.
	•	Use prove.run("quick") for self-testing before handoff.
	•	Refactorer:
	•	Use fs.read for scoped inspection.
	•	Use prove.run("quick") after refactors; escalate on deeper correctness issues.

No changes needed for Task Writer in v1.

⸻

🧱 MCP v2 — Editing + Git + Telemetry

Once v1 is stable and you like the feel of it, v2 is where MCP starts affecting how code changes land and how you observe the system.

🎯 Goal

Give Codex the ability to:
	•	Apply small edits via tools (not just via the VS Code UI)
	•	Perform safe git operations (status, diff, apply patch) in a controlled way
	•	Log all tool usage and outcomes for later analysis

v2 = Repo edits + git + run logs, still under your supervision.

⸻

🛠 New tools in v2

Add to .project/mcp/tools:
	1.	fs.write
	•	Input: { path, data }
	•	Purpose: Let Codex write files under strict policies (no infra, no secrets, etc.).
	•	Used by future orchestrator or external Codex runs; VS Code Codex can still edit normally.
	2.	git.status
	•	Input: {} or { path? }
	•	Output: { changedFiles, stagedFiles, untrackedFiles }
	•	Helps Codex understand current working state.
	3.	git.diff
	•	Input: { staged?: boolean }
	•	Output: { diff: string }
	•	Lets Refactorer “see the patch” without assuming a git CLI.
	4.	git.applyPatch
	•	Input: { patch: string }
	•	Purpose: Apply a Codex-generated diff in a controlled fashion.
	5.	telemetry.log
	•	Input: { kind, data }
	•	Writes .project/codex/logs/YYYY-MM-DD/*.ndjson
	•	Logs:
	•	which tools were called
	•	how long they took
	•	whether they succeeded
	•	Prove outcomes, etc.

⸻

🧠 How v2 changes your workflow

You now have two “paths” for changes:
	1.	VS Code path (today):
	•	Codex edits files via IDE.
	•	You run tests / commits.
	2.	MCP path (future / partial):
	•	A small script or CLI can:
	•	call Codex with task + role
	•	Codex generates patches
	•	Orchestrator applies patches via git.applyPatch
	•	prove.run verifies them
	•	You review diff + commit or even auto-commit safe ones.

Even if you don’t fully automate patch application yet, v2:
	•	Gives you visibility (via telemetry) into which tools Codex uses.
	•	Sets up the infrastructure for “one button apply patch & test”.

⸻

🧱 MCP v3 — Search/Index + Context Stack v2

v1 context is filename-only, v2 starts editing, v3 is where context becomes truly intelligent.

🎯 Goal

Let Codex navigate large repos intelligently without you manually curating context, using:
	•	code search
	•	symbol search
	•	(optionally) embeddings

v3 = Smarter, scalable context for big backends/frontends.

⸻

🛠 New tools in v3

Add to .project/mcp/tools:
	1.	code.search
	•	Input: { query, include?, exclude?, limit? }
	•	Uses ripgrep or similar to search code.
	•	Returns matches with file + line spans.
	2.	code.symbols
	•	Input: { file }
	•	Uses tree-sitter or language server data to extract:
	•	functions
	•	classes
	•	endpoints
	•	Helps locate the right function to modify.
	3.	embeddings.index (optional)
	•	Input: { op: "get" | "put", path, vector? }
	•	Backs a simple SQLite or local-store embeddings index.
	•	Later used for semantic “find similar code / usage examples.”
	4.	context.stack.build
	•	High-level tool that:
	•	runs code.search / context.select
	•	reads/summarizes a curated set of files
	•	returns a structured “Context Stack” object that Codex can drop into prompts.

⸻

🧠 How v3 changes your workflow

At this point:
	•	Task Writer produces tasks with:
	•	a “Keywords” section
	•	a “Likely modules or domains” section
	•	Builder Codex:
	•	calls context.stack.build with these keywords
	•	gets back a curated context bundle
	•	no longer needs manual file lists
	•	Refactorer Codex:
	•	gets a smaller, more precise context window
	•	can safely reason about broader impact without scanning the whole repo

You can now scale to 100k+ LOC without your workflow breaking.

⸻

🧱 MCP v4+ — Orchestrators + Repo-as-API (Future)

This is the “dream” stuff you were imagining when you thought MCP might be too futuristic. This is deliberately later.

🎯 Goal

Turn your repo + MCP + Codex roles into a full autonomous coding platform:
	•	Task comes in → agent pipeline runs → code lands → gates enforced → PR or auto-merge for safe changes.

v4+ = Autonomous pipelines, multi-agent coordination, CI integration.

⸻

🛠 New components in v4+

Not just tools, but also:
	1.	Orchestrator service / CLI
	•	Reads Task Template(s)
	•	Calls:
	•	Task Writer → Builder → Refactorer
	•	Uses MCP tools for all ops:
	•	fs.read/write
	•	git.status/applyPatch
	•	prove.run
	•	code.search/symbols
	•	Applies policies:
	•	line limits
	•	directory allowlists
	•	high-risk zones requiring human review
	2.	CI integration
	•	PR workflow:
	•	Run Prove gates via MCP
	•	Optionally invoke Refactorer Codex if gates fail
	•	Push fixup commits
	•	Merge rules:
	•	small safe changes auto-merged
	•	larger ones flagged for human review
	3.	Multi-repo / monorepo tools
	•	Tools to:
	•	navigate dependency graphs
	•	track cross-package commits
	•	update shared contracts across backend/frontend together
	4.	Policy & safety layer
	•	Configuring:
	•	which tools are allowed in which repos
	•	resource limits
	•	secrets protection and redaction
	•	Potentially:
	•	static analysis gates
	•	compliance checks

This is where “repo as an API” truly kicks in: any orchestrator, tool, or agent can safely operate on your repo under policies you define.

⸻

🎯 Where you are now & what’s next

You’re here:
	•	✅ .project/codex roles + tasks are excellent
	•	✅ Prove v1 Makefile gates are wired (prove-quick / prove-full)
	•	🚧 MCP v1: context + prove tooling is spec’d, ready to implement

Next move:
	•	Implement MCP v1 exactly as scoped:
	•	file tree
	•	4 tools
	•	server.config
	•	tiny changes to Builder/Refactorer instructions
	•	optional smoke-test scripts

Once MCP v1 is in and you’ve felt the difference in day-to-day vibe coding, we can:
	•	decide which v2 elements you want (fs.write? git.status? telemetry?),
	•	and design them in the same structured way.

If you’d like, the very next thing I can do is:
	•	give you copy-pasteable content for each MCP v1 file (server.config.yaml, package.json, the 4 tools), tailored exactly for your Python backend layout (app/, tests/, Makefile with prove-quick/full).