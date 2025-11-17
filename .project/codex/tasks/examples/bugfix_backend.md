Here is the Bugfix Task Template — optimized for Codex, Task Writer, Builder Codex, and Refactorer Codex.
It is intentionally shorter and sharper than the feature template so bugfix tasks remain surgical, safe, and non-expansive.

You can save this as:

.project/codex/tasks/templates/bugfix.md


⸻

🐛 BUGFIX TASK TEMPLATE (Codex-Ready)

Task [ID]: [Short Bug Title]

Status: Ready

⸻

🔍 Essential Context

Paste only the minimum files and snippets needed to reproduce the bug.

Examples:
	•	The failing test
	•	The error traceback
	•	The function/file where the bug originates
	•	Logs illustrating incorrect behavior

Keep this section small — limit to what Builder Codex must see.

⸻

🧠 Bug Summary (≤3 sentences)

Describe what’s broken, under what conditions, and how you know.

Example:
“Fetching user portfolios fails when the DB returns None. This throws an unhandled AttributeError. Expected behavior is to return an empty list with a 200 response.”

⸻

🎯 Goal of This Bugfix

Define the correct behavior.

Example:
“Ensure the endpoint returns an empty list instead of crashing.”

Keep it precise and measurable.

⸻

🧪 Reproduction Steps
	1.	Exact commands (pytest, curl, UI steps, etc.)
	2.	Environment variables required
	3.	Any seed data or mocks

Example:

uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
pytest tests/api/test_portfolios.py::test_empty_portfolios


⸻

❗ Acceptance Criteria

Codex must satisfy all of these:

Functional
	•	Bug is fixed and behavior matches “Goal of This Bugfix”.

Tests
	•	Add/update only the minimal tests needed.
	•	The failing test must pass after the fix.

Safety
	•	Fix must not alter public contract, schemas, or ordering unless explicitly allowed.
	•	No new features; no refactors.
	•	Touch only the files necessary to resolve the bug.

Observability
	•	If applicable, logs/errors must be improved to diagnose this bug in the future.

Docs
	•	Include a small comment if the fix clarifies intent.

⸻

🧱 Affected Files

List only the files Codex is allowed to modify.

Example:
	•	app/services/portfolio_service.py
	•	tests/api/test_portfolios.py

⸻

🔁 Inputs & Outputs (Only if applicable)

Include if the bug relates to request/response structures.

⸻

⚠️ Constraints
	•	No new abstractions or architectural changes.
	•	No large refactors, renames, or reorganizations.
	•	Fix only what you can reproduce.

⸻

📈 Business Context

(Brief; optional)

Example:
“This bug prevents users with empty portfolios from viewing any assets, causing onboarding drop-off.”

⸻

✔️ Done

This template is:
	•	fully aligned with your Builder Codex + Refactorer Codex rules
	•	optimized for exact behavior preservation
	•	tiny and surgical
	•	proven to work well in agentic pipelines
