# Complete: Prove Quality Gates (Refactored & Optimized)

## What Is It?

A **deterministic quality enforcement CLI** that runs before every commit to ensure code meets production standards. It's the **final automated gate** in your ChatGPT→Codex→Cursor→CodeRabbit workflow that prevents bad code from merging.

**Single Rule:** `No green prove, no merge.`

***

## Why Do We Have It?

**Problem:** AI agents can generate code that
- Passes tests but lacks coverage
- Works but violates best practices  
- Ships without kill-switches
- Breaks API contracts
- Has environment mismatches

**Solution:** Prove gates catch these issues **automatically** (4-6 seconds) before human review, reducing debugging time by **70%+**.

***

## How Does It Work?

```
Agent completes work (Codex/Cursor)
         ↓
Prove CLI runs (pre-commit hook)
         ↓
11 Quality Gates Execute (4-6 seconds)
         ↓
    ✅ Pass → Commit allowed → Next agent
    ❌ Fail → Block commit → Agent fixes & reruns
```

***

## 11 Core Quality Gates (Updated)

### Critical Gates (Serial - Fail Fast)

#### 1. **Trunk Enforcement** ⚙️
- **Check:** Must be on `main` branch
- **Why:** Pure trunk-based development
- **Fails if:** Working on feature branch (non-CI)

#### 2. **Mode Detection** 🎯
- **Check:** Determines Functional vs Non-Functional
- **How:** From commit message tags `[MODE:F]` or `[MODE:NF]`
- **Fails if:** No mode detected

#### 3. **Context Compliance** 📋 [T35 REFACTORED]
- **Check:** Commit message includes task ID and mode
- **Format:** `feat(scope): desc [T-2025-01-18-123] [MODE:F]`
- **Why:** Traceability + prevents context drift
- **Fails if:** Missing `[T-ID]` or `[MODE:X]`

#### 4. **Commit Message Format** ✍️
- **Check:** Conventional commits pattern
- **Format:** `type(scope): description [T-ID] [MODE:F|NF]`
- **Fails if:** Wrong format

***

### Parallel Gates (Concurrent)

#### 5. **TypeScript + Lint** 📝
- **Check:** Zero TypeScript errors, zero lint warnings
- **Fails if:** `tsc` or `eslint` errors

#### 6. **Tests** ✅
- **Check:** All tests pass
- **Fails if:** Any test fails

#### 7. **Lockfile & Engines** 🔒 [T37 - NEW]
- **Check:** `package-lock.json` matches `package.json`
- **Check:** Node version consistency
- **Why:** Prevents environment incoherence
- **Fails if:** Lockfile/version mismatch

#### 8. **API Contracts** 🔗 [T36 - NEW]
- **Check:** OpenAPI spec valid, response schemas consistent
- **Check:** No breaking API changes
- **Check:** Webhook payloads match schema
- **Why:** Prevents API contract drift
- **Fails if:** Schema removed fields, breaking changes, invalid webhooks

***

### Mode-Specific Gates (Conditional)

#### 9. **TDD Verification** (Functional Only) 🧪
- **Check:** Changed source files have corresponding tests
- **Fails if:** `src/lib/auth.ts` has no `test/lib/auth.test.ts`

#### 10. **Diff Coverage** (Functional Only) 📊
- **Check:** Changed lines have 85% coverage (60% for refactor)
- **Fails if:** Coverage below threshold

#### 11. **Kill-Switch Required** (Features Only) 🎚️
- **Check:** Feature commits include toggles/flags
- **Fails if:** Feature without kill-switch

***

## Simplified Configuration

```typescript
// prove.config.ts
export const proveConfig = {
  coverage: {
    functional: 85,      // Functional task coverage
    refactor: 60,        // Refactor task coverage
    skipNonFunctional: true
  },
  
  mode: {
    defaultMode: 'functional',
    autoDetect: true,
    envOverride: true
  },
  
  git: {
    requireMainBranch: true,
    maxCommitSize: 1000,
    enforceConventionalCommits: true
  },
  
  features: {
    requireKillSwitch: true,
    requireTests: true
  },
  
  contracts: {
    validateOpenAPI: true,
    detectBreakingChanges: true,
    validateWebhooks: true
  },
  
  lockfile: {
    enforceSync: true,
    validateEngines: true
  },
  
  parallel: {
    enabled: true,
    checks: ['typecheck', 'lint', 'tests', 'contracts', 'lockfile']
  }
}
```

***

## Simplified Commands

```bash
# Fast feedback (dev loop)
npm run prove:quick
# TypeScript, Lint, Tests, TDD, Mode
# Runtime: ~4 seconds

# Full validation (before handoff)
npm run prove
# All 11 gates
# Runtime: ~6 seconds

# JSON output (CI/CD)
PROVE_JSON=1 npm run prove
```

***

## Expected Workflow with All Gates

```
ChatGPT creates plan
         ↓ (2 min)
Codex implements
         ↓ (1 hour)
Codex runs: npm run prove:quick
         ↓ (4s)
    ✅ Trunk + Mode + Context + TDD + Coverage
    ✅ TypeScript + Lint + Tests
    ✅ Lockfile + Contracts
         ↓
Codex creates Cursor prompt
         ↓ (1 min)
Cursor refactors
         ↓ (30 min)
Cursor runs: npm run prove
         ↓ (6s)
    ✅ All 11 gates verified
         ↓
Cursor creates CodeRabbit prompt
         ↓ (1 min)
CodeRabbit reviews (security only)
         ↓ (2 min)
You review proven-safe code → Merge
         ↓ (5 min)
✅ Feature shipped with zero bugs
```

**Total your active time:** 15 minutes  
**Total agent time:** 2 hours  
**Debugging prevented:** 2-3 hours (caught automatically)

***

11 gates, with contracts + lockfile

```
Scenario: Codex changes API response schema
├─ Codex runs prove
│  └─ Contracts gate: ❌ FAIL
│     "Response schema 'LoginResponse' removed field 'token'"
│
├─ Codex fixes: Adds field back
├─ Codex reruns prove: ✅ Pass
│
├─ Cursor runs prove: ✅ Pass
├─ CodeRabbit reviews: ✅ Approved
├─ Merge to main: ✅
│
└─ Frontend works perfectly
   └─ ✅ Zero production bugs
```

***

## Summary: 11-Gate Prove Quality Gates

### What It Is
**8 core gates + 3 strategic additions** (Contracts, Lockfile, Context Compliance) that ensure AI-generated code meets production standards.

### Why We Have It
**Automates quality enforcement** so AI agents can't ship untested, unformatted, unsafe, or API-incompatible code.

### Gates Added (T35, T36, T37)

| Gate | Type | Impact | Effort |
|------|------|--------|--------|
| **T35: Context Compliance** | Critical | Traceability + prevents drift | 5 min |
| **T36: API Contracts** | Parallel | Prevents API breaking changes | 1-2 hrs |
| **T37: Lockfile & Engines** | Parallel | Prevents environment mismatches | 1-2 hrs |

### Expected Outcomes

- ✅ **70% reduction** in debugging time
- ✅ **API breaking changes prevented** (caught at commit)
- ✅ **Environment issues eliminated** (lockfile sync enforced)
- ✅ **Full traceability** (task ID in every commit)
- ✅ **6-second validation** (all 11 gates)

