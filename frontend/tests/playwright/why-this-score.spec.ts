import { expect, Locator, Page, test } from '@playwright/test';

type SignalProof = {
  source_url?: string | null;
  verified_by?: string[];
  timestamp?: string | null;
};

type BreakdownItem = {
  reason: string;
  points: number;
  proof?: SignalProof | null;
  proofs?: SignalProof[] | null;
};

type CompanyScore = {
  company_id: string;
  scoring_run_id: string;
  score: number;
  breakdown: BreakdownItem[];
  recommended_approach: string;
  pitch_angle: string;
  scoring_model: string;
  created_at: string;
  updated_at?: string | null;
};

const API_BASE_URL = process.env.API_BASE_URL ?? 'http://localhost:8000';
const COMPANY_ID =
  process.env.UI_SMOKE_COMPANY_ID ?? '11111111-0000-0000-0000-000000000001';
const COMPANY_NAME = process.env.UI_SMOKE_COMPANY_NAME ?? 'High Growth SaaS';
const SCORING_RUN_ID = process.env.UI_SMOKE_SCORING_RUN_ID ?? 'ui-smoke';
const PATCH_PROOF_MISSING =
  (process.env.UI_SMOKE_DISABLE_PROOF_PATCH ?? 'false').toLowerCase() !== 'true';

const fixtureProfile = {
  company_id: COMPANY_ID,
  name: COMPANY_NAME,
  funding_amount: '$15M',
  funding_stage: 'Series A',
  days_since_funding: 45,
  employee_count: 40,
  job_postings: 8,
  tech_stack: ['Salesforce', 'HubSpot'],
  buying_signals: ['https://press.fundsignal.dev/high-growth'],
  verified_sources: ['Exa', 'You.com', 'Tavily'],
};

let seededScore: CompanyScore | null = null;
let expectedBreakdown: BreakdownItem[] = [];
let missingProofReason: string | null =
  process.env.UI_SMOKE_EXPECTS_MISSING_PROOF_REASON ?? null;
let missingProofInjected = false;

test.beforeAll(async () => {
  seededScore = await seedFixtureScore();
  expectedBreakdown = seededScore.breakdown ?? [];
  if (!missingProofReason && expectedBreakdown.length > 0) {
    missingProofReason =
      expectedBreakdown[expectedBreakdown.length - 1]?.reason ?? null;
  }
});

test('drawer renders persisted Supabase score details', async ({ page }) => {
  if (!seededScore) {
    throw new Error('Seeded score missing; ensure scripts/seed_scores.py ran successfully.');
  }

  missingProofInjected = false;
  await maybeAttachMissingProofPatch(page);

  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];

  page.on('pageerror', (error) => consoleErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text());
    }
  });
  page.on('requestfailed', (request) => {
    failedRequests.push(
      `${request.method()} ${request.url()} — ${request.failure()?.errorText ?? 'unknown error'}`,
    );
  });

  await page.goto('/', { waitUntil: 'networkidle' });
  await openWhyThisScoreDrawer(page, COMPANY_NAME);
  const drawer = await resolveDrawer(page);

  await expectScoreSummary(drawer, seededScore.score);
  await expectRecommendedApproach(drawer, seededScore.recommended_approach);
  await expectVerifiedSources(drawer, fixtureProfile.verified_sources);

  const recognizedRows = await collectBreakdownRows(drawer, expectedBreakdown);

  expect(
    recognizedRows.length,
    'Mismatch between expected breakdown items and rendered rows',
  ).toBe(expectedBreakdown.length);

  const coveredReasons = new Set<string>();

  for (const row of recognizedRows) {
    coveredReasons.add(row.entry.reason);
    const linkLocator = row.locator.locator('a[href^="http"]');
    const linkCount = await linkLocator.count();

    if (missingProofInjected && missingProofReason === row.entry.reason) {
      expect(linkCount).toBe(0);
      await expectProofFallback(row.locator);
      continue;
    }

    expect(linkCount).toBeGreaterThan(
      0,
      `Breakdown row for "${row.entry.reason}" rendered without proof links`,
    );

    const renderedLinks = await collectLinkHrefs(linkLocator);
    const expectedLinks = collectProofUrls(row.entry);
    const overlap = renderedLinks.some((href) =>
      expectedLinks.has(normalizeUrl(href)),
    );
    expect(
      overlap,
      `Proof links for "${row.entry.reason}" did not match API payload`,
    ).toBeTruthy();

    await expectTimestampConsistency(row.text, row.entry);
  }

  for (const entry of expectedBreakdown) {
    expect(
      coveredReasons.has(entry.reason),
      `Drawer did not render breakdown reason: ${entry.reason}`,
    ).toBeTruthy();
  }

  if (missingProofInjected && missingProofReason) {
    const fallbackToast = page
      .locator('[role="alert"], [data-testid="proof-toast"]')
      .filter({ hasText: /proof/i })
      .first();
    await expect(
      fallbackToast,
      'Missing-proof toast/alert was not surfaced',
    ).toBeVisible();
  }

  expect(consoleErrors, `Console errors detected: ${consoleErrors.join('\n')}`).toEqual([]);
  expect(
    failedRequests,
    `Network failures detected: ${failedRequests.join('\n')}`,
  ).toEqual([]);
});

async function seedFixtureScore(): Promise<CompanyScore> {
  const existing = await fetchScoreIfPresent();
  if (existing) {
    return existing;
  }
  const response = await safeFetch(`${API_BASE_URL}/api/scores`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...fixtureProfile,
      scoring_run_id: SCORING_RUN_ID,
    }),
  });
  if (!response.ok) {
    throw new Error(
      `Failed to seed score fixture (${response.status} ${response.statusText}). ` +
        'Ensure FUND_SIGNAL_MODE=fixture and the API server is running.',
    );
  }
  return (await response.json()) as CompanyScore;
}

async function fetchScoreIfPresent(): Promise<CompanyScore | null> {
  const response = await safeFetch(
    `${API_BASE_URL}/api/scores/${COMPANY_ID}?scoring_run_id=${encodeURIComponent(SCORING_RUN_ID)}`,
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(
      `Score lookup failed (${response.status} ${response.statusText}). ` +
        'Run FUND_SIGNAL_MODE=fixture uvicorn app.main:app --reload.',
    );
  }
  const payload = (await response.json()) as CompanyScore[];
  return payload[0] ?? null;
}

async function safeFetch(
  url: string,
  init?: RequestInit,
  attempts = 5,
  backoffMs = 500,
): Promise<Response> {
  let lastError: Error | null = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await fetch(url, init);
    } catch (error) {
      lastError = error as Error;
      if (attempt === attempts) {
        break;
      }
      const delay = backoffMs * attempt;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
  const reason = lastError ? `: ${lastError.message}` : '';
  throw new Error(
    `Network request to ${url} failed after ${attempts} attempts${reason}. ` +
      'Ensure the FastAPI server is running (FUND_SIGNAL_MODE=fixture uvicorn app.main:app --reload).',
  );
}

async function openWhyThisScoreDrawer(page: Page, companyName: string) {
  const targetHeading = page
    .getByRole('heading', { name: new RegExp(companyName, 'i') })
    .first();
  if ((await targetHeading.count()) > 0) {
    await targetHeading.scrollIntoViewIfNeeded();
  }
  const openButton = page.getByRole('button', { name: /why this score/i }).first();
  await expect(openButton).toBeVisible();
  await openButton.click();
}

async function resolveDrawer(page: Page): Promise<Locator> {
  const drawer = page
    .locator('[data-testid="why-this-score-drawer"], [role="dialog"]')
    .filter({ hasText: /why this score/i })
    .first();
  await expect(drawer).toBeVisible({ timeout: 1_000 });
  return drawer;
}

async function collectBreakdownRows(
  drawer: Locator,
  breakdown: BreakdownItem[],
): Promise<Array<{ locator: Locator; entry: BreakdownItem; text: string }>> {
  const allRows = drawer.locator(
    '[data-testid="score-breakdown-row"], [data-test="score-breakdown-row"], [role="row"], li',
  );
  const count = await allRows.count();
  const recognized: Array<{ locator: Locator; entry: BreakdownItem; text: string }> = [];
  const usedReasons = new Set<string>();

  for (let index = 0; index < count; index += 1) {
    const candidate = allRows.nth(index);
    const text = (await candidate.innerText()).trim();
    if (!text) {
      continue;
    }
    const match = matchBreakdown(text, breakdown);
    if (!match || usedReasons.has(match.reason)) {
      continue;
    }
    usedReasons.add(match.reason);
    recognized.push({ locator: candidate, entry: match, text });
  }

  return recognized;
}

async function expectScoreSummary(drawer: Locator, expectedScore: number) {
  const numeric = String(expectedScore);
  const scoreLocator = drawer
    .locator(
      '[data-testid="score-summary"], [data-test="score-summary"], [data-testid="score-value"], [data-test="score-value"]',
    )
    .first();
  if ((await scoreLocator.count()) > 0) {
    await expect(scoreLocator).toContainText(numeric);
    return;
  }
  await expect(
    drawer.getByText(
      new RegExp(`score[^0-9]*${escapeRegExp(numeric)}(?:\\s*/\\s*100)?`, 'i'),
    ),
  ).toBeVisible();
}

async function expectRecommendedApproach(drawer: Locator, recommendation: string) {
  const recommendationLocator = drawer
    .locator('[data-testid="recommended-approach"], [data-test="recommended-approach"]')
    .first();
  if ((await recommendationLocator.count()) > 0) {
    await expect(recommendationLocator).toContainText(recommendation);
    return;
  }
  await expect(
    drawer.getByText(new RegExp(escapeRegExp(recommendation), 'i')),
  ).toBeVisible();
}

async function expectVerifiedSources(drawer: Locator, sources: string[]) {
  if (!sources.length) {
    return;
  }
  const badgeGroup = drawer
    .locator('[data-testid="verified-sources"], [data-test="verified-sources"]')
    .first();
  const target = (await badgeGroup.count()) > 0 ? badgeGroup : drawer;
  for (const source of sources) {
    await expect(
      target.getByText(new RegExp(escapeRegExp(source), 'i')).first(),
    ).toBeVisible();
  }
}

async function expectTimestampConsistency(rowText: string, entry: BreakdownItem) {
  const tokens = buildTimestampTokens(entry);
  if (!tokens.length) {
    return;
  }
  const normalizedRow = normalizeText(rowText);
  const matched = tokens.some((token) => normalizedRow.includes(token));
  expect(
    matched,
    `Timestamp tokens ${tokens.join(', ')} missing from "${entry.reason}" row`,
  ).toBeTruthy();
}

function buildTimestampTokens(entry: BreakdownItem): string[] {
  const timestamp = extractTimestamp(entry);
  if (!timestamp) {
    return [];
  }
  const parsedDate = new Date(timestamp);
  if (Number.isNaN(parsedDate.getTime())) {
    return [];
  }
  const tokens = new Set<string>();
  tokens.add(String(parsedDate.getUTCFullYear()).toLowerCase());
  tokens.add(
    parsedDate
      .toLocaleString('en-US', { month: 'short', timeZone: 'UTC' })
      .toLowerCase(),
  );
  tokens.add(
    parsedDate
      .toLocaleString('en-US', { month: 'long', timeZone: 'UTC' })
      .toLowerCase(),
  );
  tokens.add(
    parsedDate
      .toLocaleString('en-US', { day: 'numeric', timeZone: 'UTC' })
      .toLowerCase(),
  );
  return [...tokens];
}

function extractTimestamp(entry: BreakdownItem): string | null {
  if (entry.proof?.timestamp) {
    return entry.proof.timestamp;
  }
  for (const proof of entry.proofs ?? []) {
    if (proof?.timestamp) {
      return proof.timestamp;
    }
  }
  return null;
}

function matchBreakdown(rowText: string, breakdown: BreakdownItem[]) {
  const normalizedRow = normalizeText(rowText);
  return breakdown.find((entry) =>
    normalizedRow.includes(normalizeText(entry.reason)),
  );
}

function normalizeText(value: string) {
  return value.replace(/\s+/g, ' ').trim().toLowerCase();
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function collectProofUrls(entry: BreakdownItem) {
  const urls = new Set<string>();
  if (entry.proof?.source_url) {
    urls.add(normalizeUrl(entry.proof.source_url));
  }
  for (const proof of entry.proofs ?? []) {
    if (proof?.source_url) {
      urls.add(normalizeUrl(proof.source_url));
    }
  }
  return urls;
}

function normalizeUrl(value: string) {
  try {
    return new URL(value).href.replace(/\/$/, '');
  } catch {
    return value.trim();
  }
}

async function collectLinkHrefs(locator: Locator) {
  const hrefs = await locator.evaluateAll((elements) =>
    elements
      .map((element) =>
        (element as HTMLAnchorElement).href
          ? (element as HTMLAnchorElement).href
          : element.getAttribute('href') ?? '',
      )
      .filter(Boolean),
  );
  return hrefs.map(normalizeUrl);
}

async function expectProofFallback(row: Locator) {
  const fallback = row.locator(
    '[data-testid="proof-fallback"], [role="note"], [role="status"]',
  );
  if ((await fallback.count()) > 0) {
    await expect(fallback.first()).toBeVisible();
    return;
  }
  await expect(row.getByText(/proof/i).first()).toBeVisible();
}

async function maybeAttachMissingProofPatch(page: Page) {
  if (!PATCH_PROOF_MISSING || !missingProofReason) {
    return;
  }
  await page.route('**/api/scores**', async (route) => {
    if (route.request().method().toUpperCase() !== 'GET' || missingProofInjected) {
      await route.continue();
      return;
    }
    let upstreamResponse;
    try {
      upstreamResponse = await route.fetch();
    } catch {
      await route.continue();
      return;
    }
    const bodyText = await upstreamResponse.text();
    let payload: unknown;
    try {
      payload = JSON.parse(bodyText);
    } catch {
      await route.fulfill({
        status: upstreamResponse.status(),
        headers: upstreamResponse.headers(),
        body: bodyText,
      });
      return;
    }

    const mutated = removeProofsFromPayload(payload, missingProofReason);
    if (!mutated.mutated) {
      await route.fulfill({
        status: upstreamResponse.status(),
        headers: upstreamResponse.headers(),
        body: bodyText,
      });
      return;
    }

    missingProofInjected = true;

    await route.fulfill({
      status: upstreamResponse.status(),
      headers: upstreamResponse.headers(),
      body: JSON.stringify(mutated.payload),
    });
  });
}

function removeProofsFromPayload(payload: unknown, reason: string) {
  const normalizedTarget = normalizeText(reason);
  let mutated = false;

  const scrubEntry = (entry: BreakdownItem | undefined) => {
    if (!entry) {
      return;
    }
    if (normalizeText(entry.reason) === normalizedTarget) {
      entry.proof = null;
      entry.proofs = [];
      mutated = true;
    }
  };

  const scrubScore = (score: CompanyScore | undefined) => {
    if (!score?.breakdown) {
      return;
    }
    score.breakdown.forEach((entry) => scrubEntry(entry));
  };

  if (Array.isArray(payload)) {
    payload.forEach((entry) => scrubScore(entry as CompanyScore));
  } else if (payload && typeof payload === 'object') {
    scrubScore(payload as CompanyScore);
  }

  return { mutated, payload };
}
