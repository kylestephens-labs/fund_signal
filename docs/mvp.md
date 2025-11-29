# FundSignal MVP: Final Build Plan (Updated with You.com)

## 🎯 Executive Summary

**Goal:** Ship a working product in 7 days that generates $1,000 MRR within 30 days by solving the #1 pain point for B2B SaaS Account Executives: wasting 10+ hours/week manually scraping for recently funded prospects.

**What You're Building:** Multi-source verified, AI-scored lists of B2B SaaS companies that raised funding 60-90 days ago (the buying window), delivered daily via Slack/Email with explainable confidence scores and proof links.

**Differentiation:** Fastest, most transparent, multi-verified funding intelligence with continuous feedback loops—not just another lead database.

## 👥 Ideal Customer Profile

| Attribute | Specification |
|-----------|---------------|
| Job Title | Account Executive, SDR, BDR |
| Industry | B2B SaaS (selling to other SaaS companies) |
| Company Stage | Series A-C, 10-100 employees |
| Team Size | 2-10 person sales team |
| Budget | $100-$300/month for prospecting tools |
| Current Tools | Apollo (244 mentions), Clay (168), ZoomInfo (174) |
| Pain Point | Manually building lists; data always stale; can't trust accuracy |
| Where to Find Them | Reddit (r/sales), community.clay.com, LinkedIn |

## 🔥 User Pain Points We're Solving

| Pain Point | Our Solution |
|------------|--------------|
| 10+ hours/week manual prospecting | 3 AI sources auto-discover + verify daily |
| Stale data (3-4 weeks old) | Multi-source verification with timestamps |
| Can't trust lead quality | Explainable scores with proof links |
| Tool fatigue (6+ tools) | Delivered to Slack/Email (no new login) |
| Competitors get there first | 60-90 day funding window = buying mode |

## ⚡ Core Value Propositions

1. **Fastest:** Multi-source discovery (Exa + You.com + Tavily) finds companies before competitors
2. **Most Transparent:** Every lead shows "verified by [sources] on [date]" with proof links
3. **Most Accurate:** Only deliver if 2+ sources confirm (95%+ accuracy)
4. **Zero Friction:** Delivered to Slack, Email, or CSV—no new tool to learn
5. **Self-Improving:** Per-lead feedback trains model weekly

## 🏗️ Multi-Source Data Stack

| Source | Role | What It Catches | Cost |
|--------|------|-----------------|------|
| Exa | Primary discovery | First to find funding announcements via semantic web search | $150/mo |
| You.com | Mainstream press verification | Real-time news coverage from TechCrunch, PRWire, BusinessWire | $150/mo |
| Tavily | Multi-source verification | Confirms across 20+ news/blog sources | $50/mo |

**Total Monthly COGS:** $350 (at 50 customers = 95% gross margin)

**Confidence Scoring:**
- 3 sources confirm = VERIFIED ✅
- 2 sources confirm = LIKELY ⚠️
- 1 or fewer = EXCLUDE ❌

## ✨ Exceptional UX Features

### 1. Freshness Watermarks

Every lead shows:

```
Verified by: Exa, You.com, Tavily
Last checked: Oct 29, 2025, 9:15 AM
Confidence: HIGH ✅
```

FUND_SIGNAL_MODE: fixture

### 2. Explainability Drawer

Click "Why this score?" to see:

```
Score: 88/100

Breakdown:
✓ Raised $10M Series A (75 days ago) +30 pts
  → Source: TechCrunch, Oct 15
✓ Posted 5 sales roles this week +25 pts
  → Source: Greenhouse jobs page
✓ Uses Salesforce + HubSpot +20 pts
  → Source: BuiltWith
✓ 25-50 employees (founder-accessible) +13 pts

Recommended approach: Contact founder via LinkedIn
Pitch angle: "We help Series A SaaS scale outbound"

[VIEW NEWS ARTICLE] [READ ANNOUNCEMENT]
```

FUND_SIGNAL_MODE: fixture

### 3. Per-Lead Feedback

Every lead has feedback buttons:

```
Was this lead relevant?
[✓ Yes] [⊗ Not now] [✗ Wrong fit]
```

Feedback trains next batch + triggers support for bad leads.

FUND_SIGNAL_MODE: fixture

### 4. Concierge Backstop

"Missed a company? Paste link below → we'll enrich it within 24h"

- Turns data gaps into loyalty moments
- Shows you hustle for outcomes

FUND_SIGNAL_MODE: fixture

### 5. Multi-Channel Delivery

Choose your format:
- Slack alert (Monday 9 AM)
- Email digest (HTML with cards)
- CSV/Airtable export (one-click download)

No forced Slack adoption—users get value immediately.

FUND_SIGNAL_MODE: online (requires Slack/email integrations)

### 6. Transparent Trial Timeline

When a prospect adds a card, `POST /billing/subscribe` responds with `trial_start`, `trial_end`, and `current_period_end` plus the Stripe `client_secret`, enabling the frontend to render a 14-day countdown before billing. This clarity boosts conversion while keeping trust high.

### 7. Stripe Webhook Reliability

`POST /billing/stripe/webhook` is registered in Stripe dashboard and verified with the Stripe CLI. Every `checkout.session`, `invoice.payment_*`, and `customer.subscription.*` event lands in our `subscriptions` + `processed_events` tables so operators can trust the billing status shown in-product.

## 💰 Pricing Model

| Tier | Price | What's Included |
|------|-------|-----------------|
| Starter | $149/mo | 25 verified companies/week, Slack + Email delivery, basic signals |
| Pro | $247/mo | 75 companies/week, full enrichment, Airtable sync, priority support |
| Team | $499/mo | Unlimited companies, CRM integration, API access, dedicated support |

**Early Adopter Promo:** First 50 users get $49/mo for life (67% off)

## 🏰 Moat (Defensibility)

| Layer | How It Works | Timeline |
|-------|--------------|----------|
| Multi-source verification | Competitors use 1 DB; we use 3+ sources | Day 1 |
| Feedback-trained model | Weekly tuning based on which leads convert | Week 2+ |
| Transparency advantage | Show proof links; competitors hide sources | Day 1 |
| Concierge backstop | Manual enrichment within 24h; competitors = automated only | Day 1 |
| Workflow integration | Delivered where users already work (Slack/Email) | Day 1 |

***

# 7-Day MVP Build Plan

## Day 1: Multi-Source Data Pipeline

**Goal:** Prove all 3 sources work together and cross-verify

**Tasks:**

### 1. Set up Exa Webset (2 hours)

```python
exa_results = exa.search_and_contents(
    query="B2B SaaS companies announced seed Series A Series B funding last 60-90 days",
    type="neural",
    num_results=50
)

# Extract: company name, funding amount, date, source URL
```

FUND_SIGNAL_MODE: online

### 2. Set up You.com News API verification (2 hours)

```python
for company in exa_results:
    youcom_verify = youcom.news_search(
        query=f"{company.name} raised {company.funding_amount} funding",
        num_results=10
    )
    
    if youcom_verify.results >= 2:  # At least 2 news articles confirm
        company.youcom_verified = True
        company.news_sources = [article.publisher for article in youcom_verify.results]
```

FUND_SIGNAL_MODE: online

### 3. Set up Tavily confirmation (2 hours)

```python
for company in verified_companies:
    tavily_result = tavily.search(
        query=f"{company.name} {company.funding_stage} funding announcement",
        max_results=10
    )
    
    if tavily_result.source_count >= 2:  # Multiple sources confirm
        company.tavily_verified = True
        company.proof_links = tavily_result.top_sources
```

FUND_SIGNAL_MODE: online

### 4. Build confidence scoring (1 hour)

```python
sources_confirmed = 0
if company.exa_found: sources_confirmed += 1
if company.youcom_verified: sources_confirmed += 1
if company.tavily_verified: sources_confirmed += 1

if sources_confirmed >= 3:
    company.confidence = "VERIFIED ✅"
elif sources_confirmed >= 2:
    company.confidence = "LIKELY ⚠️"
else:
    company.confidence = "EXCLUDE ❌"
```

FUND_SIGNAL_MODE: fixture (runs on captured data)

**Definition of Done:**
- ✅ 50+ B2B SaaS companies discovered
- ✅ Each has confidence score (VERIFIED/LIKELY/EXCLUDE)
- ✅ Each shows "Verified by: [sources]" with timestamps
- ✅ Only VERIFIED + LIKELY companies move to next step

## Day 2: AI Scoring + Explainability

**Goal:** Every lead has explainable score with proof links

**Tasks:**

### 1. Build ChatGPT scoring engine (3 hours)

```python
for company in verified_companies:
    prompt = f"""
    Score this B2B SaaS company 0-100 for sales readiness:
    
    Company: {company.name}
    Funding: {company.funding_amount} {company.funding_stage}
    Days since funding: {company.days_since_funding}
    Employees: {company.employee_count}
    Open sales roles: {company.job_postings}
    Tech stack: {company.tech_stack}
    
    Scoring rubric (show math):
    1. Funding recency (0-90 days = +30 pts)
    2. Hiring velocity (5+ roles = +25 pts)
    3. Tech stack fit (Salesforce/HubSpot = +20 pts)
    4. Team size (25-50 = +15 pts)
    5. Buying signals (recent press = +10 pts)
    
    Return JSON with:
    - score (0-100)
    - breakdown (array of {reason, points, source_url})
    - recommended_approach
    - pitch_angle
    """
    
    result = openai.chat(prompt)
    company.score = result['score']
    company.breakdown = result['breakdown']
```

FUND_SIGNAL_MODE: online (requires OpenAI API)

### 2. Add proof links to every signal (2 hours)

```python
# Each breakdown item includes source URL
{
    "reason": "Raised $10M Series A (75 days ago)",
    "points": 30,
    "source_url": "https://techcrunch.com/...",
    "verified_by": ["Exa", "You.com", "Tavily"],
    "timestamp": "2025-10-29T09:15:00Z"
}
```

FUND_SIGNAL_MODE: fixture (uses captured evidence)

**Definition of Done:**
- ✅ Every company has score 0-100
- ✅ Every score shows breakdown with proof links
- ✅ Scores align with intuition (funded + hiring = high)
- ✅ "Why this score?" expandable drawer works

## Day 3: Email Delivery

**Goal:** Users can get value via automated email digests.

**Tasks:**

### 1. Build email digest (2 hours)
- Same format as Slack but HTML
- Include CSV download link at top

FUND_SIGNAL_MODE: fixture (HTML rendering)
Repo: Backend

### 2. Set up email automation (2 hours)

```
Trigger: Monday 9 AM Pacific

Step 1: Pull top 25 VERIFIED companies
Step 2: Render HTML email digest
Step 3: Send to customer distribution list
```

FUND_SIGNAL_MODE: online (requires email connection)
Repo: Backend

**Definition of Done:**
- ✅ Email digest sends Monday 9 AM
- ✅ Email content matches latest scoring data
- ✅ Zero manual work required

**FSQ-036C Implementation Testing and Notes**
- Persist scores in Supabase/Postgres first: `uv run python scripts/seed_scores.py --fixture tests/fixtures/scoring/regression_companies.json --scoring-run demo-day3 --seed-all --force` hydrates the end-to-end smoke run after an API restart.
- Delivery adapters read directly from the database via `python -m pipelines.day3.email_delivery --scoring-run demo-day3` and `python -m pipelines.day3.slack_delivery --scoring-run demo-day3`, logging `delivery.supabase.query` so ops can verify reads on Supabase dashboards.
- `.env.example` now documents `DELIVERY_SCORING_RUN`, `DELIVERY_FORCE_REFRESH`, `EMAIL_FROM`, `EMAIL_SMTP_URL`, `SLACK_WEBHOOK_URL`, and `DELIVERY_OUTPUT_DIR` so staging/prod jobs can toggle force=true behavior, webhook targets, and output locations without editing the scripts.

**FSQ-037A Implementation Testing and Notes**
- `pipelines.day3.email_delivery` exposes `--deliver`; keep running `uv run python -m pipelines.day3.email_delivery --output output/email_demo.md --deliver` once `EMAIL_SMTP_URL`, `EMAIL_FROM`, and `EMAIL_TO` are set (Mailtrap/Papercut recommended for staging). Pass `--no-deliver` to suppress a send when `DELIVERY_EMAIL_FORCE_RUN=true`.
- The CLI writes Markdown first, then sends via SMTP, logs `delivery.email.sent`, and records a latency metric so Render/GitHub monitors can confirm emails were transmitted.
- `.env.example`/README outline `EMAIL_TO`, `EMAIL_CC`, `EMAIL_BCC`, `EMAIL_SUBJECT`, `EMAIL_DISABLE_TLS`, and `DELIVERY_EMAIL_FORCE_RUN`; the last one lets cron jobs opt into send mode without passing `--deliver` every time.

**FSQ-036D Implementation Testing and Notes**
- `make ui-smoke-seed` wraps `scripts/seed_scores.py` so operators can rehydrate the UI smoke persona (`UI_SMOKE_COMPANY_ID`, `UI_SMOKE_SCORING_RUN_ID`) with a single command; each insert logs `ui_smoke.seed.success` to help correlate with Supabase dashboards.
- Playwright lives in `frontend/tests/playwright/why-this-score.spec.ts`; run `npx playwright test --config frontend/tests/playwright.config.ts --grep "Why this score"` after seeding and starting both the API (`API_BASE_URL`) and UI (`UI_BASE_URL`) to assert the drawer renders persisted score, proof links, verified sources, timestamps, and recommended approach copy.
- `.github/workflows/ui-smoke.yml` is opt-in gated via `CI_UI_SMOKE_ENABLED`; once the repo/org secrets include the UI smoke env vars + `DATABASE_URL`, the workflow installs browsers, seeds the deterministic record, runs the drawer spec, and uploads Playwright traces/screenshots/videos whenever the suite fails.

## Day 4: Feedback Loop + Concierge

**Goal:** Build continuous improvement + human backstop

**Tasks:**

### 1. Add per-lead feedback buttons (2 hours)

```
Was this lead relevant?
[✓ Relevant] [⊗ Not now] [✗ Wrong fit]

(Optional) Tell us why: [text input]
```

FUND_SIGNAL_MODE: fixture (UI + API mocks)
Repo: Frontend

### 2. Build feedback tracking (2 hours)

```python
# Store feedback in DB
feedback = {
    "company_id": company.id,
    "user_id": user.id,
    "rating": "relevant" | "not_now" | "wrong_fit",
    "comment": user_comment,
    "timestamp": now()
}

# Weekly analysis
if company.wrong_fit_count > 3:
    exclude_from_future_batches()

if signal.wrong_fit_correlation > 0.7:
    reduce_signal_weight()
```

FUND_SIGNAL_MODE: fixture (local DB + seed data)
Repo: Backend

### 3. Build "Missed lead?" form (2 hours)

```
Missed a company we should have found?

Paste announcement link: [input]

We'll enrich it and add to your list within 24h.
```

FUND_SIGNAL_MODE: fixture (form + local queue)
Repo: Frontend

### 4. Set up manual enrichment workflow (1 hour)

```
When user submits link:
1. Alert in your Slack: "User X submitted link: [URL]"
2. You manually verify + enrich company
3. Add to their next batch with "User-requested" tag
4. Send them Slack notification: "We added [Company] based on your request"
```

FUND_SIGNAL_MODE: online (needs Slack + notification hooks)
Repo: Backend

**Definition of Done:**
- ✅ Feedback buttons work on every lead
- ✅ Feedback data flows to database
- ✅ "Missed lead?" form works
- ✅ You get Slack alerts for manual enrichment requests
- ✅ Can complete manual enrichment in <30 min

## Day 5: Landing Page + Signup Flow

**Goal:** Convert visitors to trial signups in <60 seconds

**Tasks:**

### 1. Build landing page (3 hours - use Carrd.co)

**Headline:** "Stop Wasting 10 Hours/Week on Stale Lead Lists"

**Subheadline:** "Get 25 recently-funded B2B SaaS prospects—verified by 3 sources—delivered to your Slack every Monday morning."

**Social Proof:**

```
Apollo costs $399/month. Their data is 3-4 weeks old.
We're $149/month. Our data refreshes daily.

Every lead verified by: Exa + You.com + Tavily
With proof links and explainable scores.
```

**Features:**
- ✓ Multi-source verified (Exa + You.com + Tavily)
- ✓ Explainable AI scores (see why each lead matters)
- ✓ Delivered to Slack/Email (no new login)
- ✓ Proof links included (TechCrunch, BusinessWire)
- ✓ 24h concierge (we'll find companies you request)

**CTA:** "Start 14-Day Free Trial"

**Guarantee:** "If you don't book 2 meetings in your first month, we'll refund you"

FUND_SIGNAL_MODE: fixture (Carrd/content only)
Repo: Frontend

### 2. Build signup flow (2 hours)

```
Step 1: Enter email
Step 2: See 5 sample prospects instantly (no login)
Step 3: Choose delivery: Slack / Email / Both
Step 4: Enter payment (14-day trial, no charge today)
Step 5: Get first prospects within 5 minutes
```

FUND_SIGNAL_MODE: fixture (local mock signup)
Repo: Frontend

### 3. Set up Stripe checkout (1 hour)
- Solo: $99/mo
- Team: $149/mo
- Growth: $249/mo
- 14-day free trial
- Cancel anytime

FUND_SIGNAL_MODE: online (Stripe test/live keys)
Repo: Backend

**Definition of Done:**
- ✅ Landing page live at fundsignal.com
- ✅ Signup takes <60 seconds
- ✅ Users see 5 sample prospects before paying
- ✅ Stripe trial configured correctly
- ✅ First prospects delivered within 5 minutes

## Day 6: Beta Launch (Community-First)

**Goal:** Get first 10 trial signups from community

**Tasks:**

### 1. Launch in community.clay.com (1 hour)

**Title:** "Built an alternative to Apollo with daily data + multi-source news verification"

**Body:**
```
"I got tired of Apollo's stale data (3-4 weeks old) so I built FundSignal:

- Daily discovery via Exa semantic search
- Verified by You.com News + Tavily (20+ sources)
- Shows proof links for every claim
- Explainable AI scores (not black-box)
- Delivered to Slack/Email (no dashboard fatigue)

Free 14-day trial: fundsignal.com

First 50 users get $49/mo for life (normally $149)

Would love feedback from Clay community!"
```

FUND_SIGNAL_MODE: online (requires posting to communities)
Repo: Frontend

### 2. Launch in r/sales (1 hour)

**Title:** "Built a tool that finds recently funded companies with multi-source news proof"

**Body:**
```
"Sales problem: You find a company on Apollo, they 'raised funding' 6 weeks ago.
You reach out... turns out competitors contacted them weeks ago.

FundSignal solves this:
- Multi-source verified (Exa + You.com + Tavily)
- Every lead shows news articles confirming funding
- Targets 60-90 day window (when they're actually buying)
- Daily refresh (not weekly like Apollo)

Free trial: fundsignal.com
First 10 users get $49/mo for life."
```

FUND_SIGNAL_MODE: online (Reddit post)
Repo: Frontend

### 3. Direct outreach (2 hours)
- Message 20 AEs/SDRs on LinkedIn
- "Hey [Name], testing a new prospecting tool—takes 5 min to set up, shows recently funded companies with verified news coverage. Curious for your feedback?"

FUND_SIGNAL_MODE: online (LinkedIn messaging)
Repo: Frontend

**Definition of Done:**
- ✅ 3 community posts live
- ✅ 20 direct messages sent
- ✅ First trial signup received
- ✅ No paid ads (pure community)

## Day 7: Onboard + Iterate

**Goal:** Get first users to value in <5 minutes + collect feedback

**Tasks:**

### 1. Manual onboarding (3 hours)
- Welcome email with 2-min setup video
- Slack DM: "Your first 5 prospects are ready—click for explainability"
- Schedule 15-min call with each user

FUND_SIGNAL_MODE: online (real users)
Repo: Frontend

### 2. Monitor engagement (2 hours)
- % who click Slack alert
- % who expand "Why this score?"
- % who click feedback buttons
- % who submit "missed lead" requests

FUND_SIGNAL_MODE: online (live telemetry)
Repo: Backend

### 3. Iterate immediately (3 hours)
- Fix #1 complaint within 24h
- Update ChatGPT prompt if scores are off
- Add most-requested feature

FUND_SIGNAL_MODE: fixture (implement fixes locally, then redeploy)
Repo: Backend

**Definition of Done:**
- ✅ 10 trial signups onboarded
- ✅ 5+ users engaged with first alert
- ✅ 1+ users say "I'd pay for this"
- ✅ Feedback collected + documented
- ✅ Week 2 iteration plan ready

***

## Success Metrics (End of Week 1)

| Metric | Target | Stretch |
|--------|--------|---------|
| Trial signups | 10 | 25 |
| Engagement rate (% who click alert) | 60% | 80% |
| "Would pay" responses | 3 | 10 |
| Data accuracy (user-verified) | 90% | 95% |
| Time to first value | <5 min | <2 min |
| Feedback submissions | 5+ | 15+ |

## Unit Economics

| Metric | Value |
|--------|-------|
| Monthly COGS (Exa + You.com + Tavily) | $350 |
| Target customers (Month 1) | 10 |
| Blended ASP | $149 |
| Monthly revenue | $1,490 |
| Gross margin | 77% |
| Net profit (Month 1) | $1,140 |

**At 50 customers:**
- Revenue: $7,450/month
- COGS: $350
- Gross margin: 95%
- Net profit: $7,100/month

## Week 2+ Roadmap

**Goal:** Convert 3 trial users to paying ($447 MRR base)

**Focus:**
1. Daily check-ins with trial users
2. Fix top 3 complaints from Week 1
3. Add most-requested integration (Salesforce or HubSpot)
4. Launch referral: "Invite teammate, both get +10 leads/week"

# Post-MVP

### Build Slack alert format (2 hours)

```
🔔 FundSignal • 25 New Prospects • Week of Oct 29

1. [Company Name] (Score: 88 - CONTACT THIS WEEK)
   
   Freshness: Funding 75 days ago • Confidence: VERIFIED ✅ • Last verified: Oct 29
   
   Why now:
   ✓ Raised $10M Series A (TechCrunch, BusinessWire)
   ✓ Posted 5 sales roles (Greenhouse)
   ✓ Uses Salesforce + HubSpot (BuiltWith)
   
   Recommended: Contact founder via LinkedIn
   Pitch: "We help Series A SaaS scale outbound"
   
   [CREATE CRM TASK] [WHY THIS SCORE?] [NOT RELEVANT]

2. [Next company]...
```

FUND_SIGNAL_MODE: fixture (formatting only)
Repo: Backend

### Build Airtable/CSV export (1 hour)
- One-click "Download this week's prospects"
- Columns: Company, Score, Confidence, Verified By, Funding, Hiring, Tech Stack, Proof Links

FUND_SIGNAL_MODE: fixture (generate exports from captured bundle)
Repo: Backend

### Set up Slack + CSV automation (2 hours)

```
Trigger: Monday 9 AM Pacific

Step 1: Pull top 25 VERIFIED companies
Step 2: Format Slack message
Step 3: Send to customer Slack workspace
Step 4: Send email digest
Step 5: Generate CSV, upload to Airtable
```

FUND_SIGNAL_MODE: online (requires Slack/email/Airtable connections)
Repo: Backend

---

You have the plan. Now ship it in 7 days.

Ready to start Day 1?
