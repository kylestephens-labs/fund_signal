1. How MCP v1 fits your current roles

From your current roles:
	•	Builder is already required to:
	•	implement the Task Template exactly as written
	•	run make prove-quick before handoff and report command + result ￼
	•	Refactorer is required to:
	•	stay strictly inside Builder’s footprint
	•	rerun make prove-quick and report command + result
	•	Task Writer:
	•	always bakes make prove-quick / make prove-full into the task template
	•	treats those gates as part of the contract for Builder/Refactorer

So your system is already very “gate-focused” and “template-driven”. MCP v1 just gives your Codex roles a better way to do what they’re already supposed to do:
	•	Context tools (so Builder can find & summarize relevant code instead of you pasting it)
	•	Read tool (so it can inspect any file cleanly)
	•	Prove tool (so Builder/Refactorer can run prove-quick on their own)

No repo-as-API, no git, no write access in v1. Just: better context + self-testing.

⸻

2. Exact MCP v1 file tree

From your backend repo root:

.project/
  mcp/
    server.config.yaml
    package.json           # (for MCP tool deps)
    tools/
      fs.read.ts
      context.select.ts
      context.summarize.ts
      prove.run.ts

We’re intentionally limiting to 4 tools in v1:
	1.	fs.read – read a file’s contents
	2.	context.select – pick likely-relevant files for a given task
	3.	context.summarize – get head/tail or first N lines of a file
	4.	prove.run – call make prove-quick / make prove-full and return structured results

No fs.write, no git.exec yet. VS Code/Codex continues to own editing & git; MCP just augments context + tests.

⸻

3. MCP v1 tools (80/20 implementations)

Assume you’ll run these with Node 18+/20+ and ts-node during development.

3.1 .project/mcp/tools/fs.read.ts

Read a file (UTF-8), return structured data:

import { readFile } from "node:fs/promises";

type FsReadParams = {
  path: string;
};

type FsReadResult = {
  path: string;
  data: string;
};

export default async function fsRead(params: FsReadParams): Promise<FsReadResult> {
  const { path } = params;
  const data = await readFile(path, "utf8");
  return { path, data };
}


⸻

3.2 .project/mcp/tools/context.select.ts

Very simple “relevant files for this task” selector using filename matches.
80/20: look at paths only; you can get fancy later.

import globby from "globby";

type ContextSelectParams = {
  task: string;
  include?: string[];
  exclude?: string[];
  limit?: number;
};

type ContextSelectResult = {
  files: string[];
};

export default async function contextSelect(params: ContextSelectParams): Promise<ContextSelectResult> {
  const {
    task,
    include = ["app/**/*.py", "tests/**/*.py"],
    exclude = ["**/.venv/**", "**/.git/**", "**/__pycache__/**"],
    limit = 24,
  } = params;

  const files = await globby(include, { ignore: exclude });

  const terms = task
    .toLowerCase()
    .split(/[^a-z0-9_]+/g)
    .filter(Boolean);

  const scored = files
    .map((p) => ({
      path: p,
      score: scorePath(p.toLowerCase(), terms),
    }))
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((x) => x.path);

  return { files: scored };
}

function scorePath(path: string, terms: string[]): number {
  return terms.reduce((sum, term) => (path.includes(term) ? sum + 1 : sum), 0);
}

Later, you can add:
	•	token-based scoring using file contents
	•	semantic search via embeddings

But this is enough to keep Codex from scanning the whole repo blindly.

⸻

3.3 .project/mcp/tools/context.summarize.ts

Return the first N lines as a cheap summary (you can extend to head+tail later).

import { readFile } from "node:fs/promises";

type ContextSummarizeParams = {
  path: string;
  maxLines?: number;
};

type ContextSummarizeResult = {
  path: string;
  summary: string;
  totalLines: number;
};

export default async function contextSummarize(
  params: ContextSummarizeParams
): Promise<ContextSummarizeResult> {
  const { path, maxLines = 200 } = params;
  const text = await readFile(path, "utf8");
  const lines = text.split(/\r?\n/);
  const summaryLines = lines.slice(0, maxLines);
  return {
    path,
    summary: summaryLines.join("\n"),
    totalLines: lines.length,
  };
}

Codex can then:
	•	call context.select → get candidate files
	•	call context.summarize → bring in just the top of each file into its prompt

That keeps you well within token budgets.

⸻

3.4 .project/mcp/tools/prove.run.ts

Bridge to your existing Makefile gates (prove-quick / prove-full):

import { execa } from "execa";

type ProveRunParams = {
  mode: "quick" | "full";
};

type ProveRunResult = {
  mode: "quick" | "full";
  command: string;
  exitCode: number;
  ok: boolean;
  stdout: string;
  stderr: string;
};

export default async function proveRun(params: ProveRunParams): Promise<ProveRunResult> {
  const { mode } = params;

  const target = mode === "full" ? "prove-full" : "prove-quick";
  const command = `make ${target}`;

  const { stdout, stderr, exitCode } = await execa("make", [target], {
    shell: false,
  });

  return {
    mode,
    command,
    exitCode,
    ok: exitCode === 0,
    stdout,
    stderr,
  };
}

Now Builder/Refactorer don’t need to “know” pytest command details – they just call prove.run.

⸻

4. Minimal server.config.yaml

Place in .project/mcp/server.config.yaml:

server:
  name: fund-signal-mcp
  transport: stdio
  workingDir: .

limits:
  maxExecMs: 300000   # 5 minutes
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

That’s the whole MCP v1 “catalog”.

⸻

5. Small updates to Builder & Refactorer roles to use MCP

Your roles are already very polished; we just need to add MCP awareness, not rewrite anything.

5.1 Builder Codex – add MCP usage section

In role_builder.md, after Automation Loop or Prove Quality Gates, add something like:

MCP Tool Usage (if available)
	•	When assembling context, prefer MCP tools over manual file copying:
	•	Call context.select with the Task Template text to choose candidate files.
	•	Call context.summarize and/or fs.read on those files to inspect their contents before editing.
	•	When verifying your implementation, prefer the MCP tool prove.run with mode: "quick" instead of assuming a terminal:
	•	Treat ok: false or non-zero exitCode as a failing gate; inspect stdout/stderr, fix the issue, and rerun until ok: true.
	•	If MCP tools are not available, fall back to the explicit commands in the Task Template and Prove docs.

That keeps your existing Prove instructions intact, just gives Builder a better mechanism to honor them.

⸻

5.2 Refactorer Codex – add MCP usage section

In role_refactorer.md, after Deterministic Ritual or Prove Quality Gates, add:

MCP Tool Usage (if available)
	•	Use fs.read to inspect Builder’s touched files and any directly-invoked helpers; do not widen scope beyond the defined boundaries.
	•	When validating your changes, call prove.run with mode: "quick" and report the command, ok value, and any failing output in your summary.
	•	Do not treat a failing prove.run as permission to expand scope; if the failure reflects a deeper correctness issue beyond Builder’s patch, stop and escalate instead of rewriting large areas.
	•	If MCP tools are not available, fall back to the default quick-test commands (uv venv, uv pip install, make prove-quick).

This keeps Refactorer constrained and behavior-preserving, but lets it self-test more cleanly.

Task Writer doesn’t really need MCP awareness for v1; it already encodes the Prove gates and commands explicitly.

⸻

6. Step-by-step install & test procedure

Here’s a concrete checklist you can follow.

Step 0 – Prereqs
	•	Node.js 18+ or 20+ installed (node -v)
	•	npm available

Step 1 – Create MCP folder & config

From repo root:

mkdir -p .project/mcp/tools

Add:
	•	.project/mcp/server.config.yaml (from section 4)
	•	.project/mcp/tools/fs.read.ts
	•	.project/mcp/tools/context.select.ts
	•	.project/mcp/tools/context.summarize.ts
	•	.project/mcp/tools/prove.run.ts

Step 2 – Add package.json for MCP tools

In .project/mcp/package.json:

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

Then install:

cd .project/mcp
npm install
cd ../..

Step 3 – Smoke-test prove.run manually

Create a quick debug script at .project/mcp/test-prove-run.ts:

import proveRun from "./tools/prove.run.js";

async function main() {
  const res = await proveRun({ mode: "quick" });
  console.log(JSON.stringify(res, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

Then run:

cd .project/mcp
npx ts-node test-prove-run.ts

You should see JSON with:
	•	mode: "quick"
	•	command: "make prove-quick"
	•	ok: true/false
	•	stdout, stderr

If ok is false due to failing tests, that’s fine—the wiring is correct.

Step 4 – Smoke-test context.select / fs.read

Similarly, you can make a tiny script like:

import contextSelect from "./tools/context.select.js";
import fsRead from "./tools/fs.read.js";

async function main() {
  const task = "update portfolio API endpoint and tests";
  const { files } = await contextSelect({ task });
  console.log("Selected files:", files);

  if (files[0]) {
    const file = await fsRead({ path: files[0] });
    console.log("First file snippet:\n", file.data.slice(0, 500));
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

Run with npx ts-node again to confirm it works.

Step 5 – Update role files
	•	Edit .project/codex/roles/builder.md to add the MCP usage section.
	•	Edit .project/codex/roles/refactorer.md similarly.

You don’t need to change Task Writer or the templates for MCP v1.

⸻
