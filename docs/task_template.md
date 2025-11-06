You are an expert software architect working on a Multi-source verified, AI-scored lists of B2B SaaS companies that raised funding 60-90 days ago. This project will involve multiple ai agents

Review the project file mvp.md to gain context on the project

Next review everything under day 1 in mvp.md.

Now create all of the day 1 tasks using this task template:

### FINAL OPTIMIZED TASK TEMPLATE (Codex-Ready)

Task [ID]: [Title]
	•	Status: [Ready/In Progress/Completed]

⸻

📚 ESSENTIAL CONTEXT

CRITICAL: Read these before coding:
	•	docs/mvp.md

    ⸻

🧠 Quick Overview (≤3 sentences)

Why this task exists, what it delivers, and the high-level outcome.
Keep under 200 words so Codex can understand purpose immediately.

⸻


🎯 Goal of the Task

Clear, outcome-driven statement of what success looks like.

⸻

🚀 Run This (Local Dev & Test Commands)

Exact commands to run the task end-to-end.

uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
# To start database locally, if needed:
docker compose up -d db
# Start API server
uvicorn app.main:app --reload
# Run tests
pytest

Include environment variables, mock setup, or seed data if required.

⸻

🧰 Local Development
	•	Setup: Commands and services needed
	•	Seed Data: What data gets created and why
	•	Env Vars: .env.example updated
	•	Common Issues: Troubleshooting notes

⸻

Development Practices
	•	Trunk-based: Work directly on main branch
	•	Commit limit: ≤1000 lines per commit
	•	Independent tasks: Each must run/test in isolation

⸻

🧪 BDD Scenario

Feature: [Feature Name]
As a [user type]
I want to [goal]
So that [benefit]

Scenario: [Main scenario]
Given [precondition]
When [action]
Then [expected result]

⸻

✅ Acceptance Criteria

Each must be testable and map directly to a test or check.
	•	Functional: [Expected behavior or outcome]
	•	Error Handling: [All API errors (from Exa, You.com, or Tavily) are logged with context; user receives actionable message]
	•	Performance: [Latency/throughput targets, e.g. P95 < 300ms]
	•	Security: [API keys never printed to log output or returned in API responses]
	•	Observability: [Success and error logs visible in Render dashboard; DB insertions confirmed in Supabase]
	•	Documentation: [README or inline docs updated]

⸻

⚙️ Files & Resources
	•	Files Affected: [List of source files to create/modify]
	•	Dependencies: [Blocking tasks, env vars, services]
	•	External Resources: Exa API, Tavily API, You.com API docs, Render.com dashboard, Supabase dashboard
	•	Contracts/IO Shapes: [Request/Response examples or schemas]

⸻

🧱 Inputs & Outputs (Contracts)
	•	Request: Example JSON or type definition
	•	Response: Expected JSON or schema
	•	Error Codes: Explicit list with messages
	•	Idempotency/Versioning: Strategy to prevent duplicates or drift

⸻

💼 Business Context
	•	Value: Why this matters to the business
	•	Risk: What could go wrong
	•	Success Metrics: How to know it’s done and high quality
    •	Hypothesis: “Multi-source, timestamped, explainable leads convert faster and yield higher user trust.

    ###REVIEW AND VALDIATE YOUR WORK MEETS THE CRITERIA IN docs/delivery/dod_testing.md BEFORE CONSIDERING BEING DONE WITH THE TASK.

