# GTM Agent

Open-source autonomous sales prospecting agent. Discovers prospects via Apollo, enriches them with Claude, syncs to HubSpot, and posts actionable Slack cards with one-click sequencing.

Built for founders and sales teams who want to run outbound on autopilot.

## What It Does

```
Apollo Search (5 pages/ICP) -> Company Filter -> Dedup (Supabase) -> Claude Enrichment -> HubSpot Sync -> Slack Digest
                                                                                                              |
                                                                                          Button clicks trigger:
                                                                                          - Apollo sequence enrollment
                                                                                          - HubSpot deal stage updates
                                                                                          - Scoring feedback loop
```

Every day, the agent:

1. Searches Apollo for prospects matching your ICP (title, seniority, company size)
2. Filters results against your target company list + relevant industries
3. Deduplicates against Supabase (already seen) and HubSpot (already contacted)
4. Enriches each prospect with Claude (research brief, personalization hook, priority score 1-10)
5. Creates contacts and deals in HubSpot
6. Posts individual prospect cards to Slack with action buttons

You click a button, and it handles the rest:

| Button | What Happens |
|--------|-------------|
| Sequence | Enriches email via Apollo, creates contact, enrolls in Apollo email sequence, updates HubSpot deal to "Sequenced" |
| LinkedIn | Marks for LinkedIn outreach, updates HubSpot deal to "Sequenced" |
| I'll Handle | Marks as manually handled, moves HubSpot deal to "Engaged" |
| Bad Fit | Saves feedback so Claude learns to deprioritize similar prospects |

## Architecture

Two Railway services from the same repo:

| Service | Type | Purpose |
|---------|------|---------|
| GTM Cron | Cron job | Daily prospect discovery pipeline |
| Actions Service | Always-on | Slack button handler (webhook receiver) |

### Files

```
gtm-agent/
├── main.py               # Pipeline orchestrator (cron entry point)
├── slack_actions.py       # Slack interaction handler (always-on)
├── config.py              # ICP definitions, company identity, env vars
├── apollo_client.py       # Apollo search, company filtering, sequencing
├── enrichment.py          # Claude-powered research briefs + scoring
├── hubspot_client.py      # Contact/deal creation, stage updates
├── slack_notify.py        # Slack digest with prospect cards + buttons
├── state.py               # Supabase state store (dedup, page tracking)
├── feedback_sync.py       # Syncs engagement signals back from Apollo/HubSpot
├── resend_slack.py        # Utility: resend digest for a specific date
├── requirements.txt       # Python dependencies
└── railway.json           # Railway build config
```

## Setup Guide

### Prerequisites

You need accounts (free tiers work to start) for:

- **Apollo** (prospect search + email sequences)
- **Anthropic** (Claude API for enrichment)
- **HubSpot** (CRM, free tier works)
- **Slack** (workspace with a bot app)
- **Supabase** (state store, free tier)
- **Railway** (hosting, ~$5/month)

### Step 1: Supabase Tables

Create three tables in your Supabase project:

**gtm_prospects**
```sql
CREATE TABLE gtm_prospects (
    id TEXT PRIMARY KEY,
    apollo_id TEXT,
    email TEXT,
    name TEXT,
    company TEXT,
    linkedin_url TEXT,
    icp_key TEXT,
    priority_score INTEGER,
    status TEXT DEFAULT 'discovered',
    enrichment JSONB,
    decision JSONB,
    email_opened BOOLEAN DEFAULT FALSE,
    email_replied BOOLEAN DEFAULT FALSE,
    email_clicked BOOLEAN DEFAULT FALSE,
    email_bounced BOOLEAN DEFAULT FALSE,
    outcome TEXT,
    deal_stage TEXT,
    deal_stage_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

**gtm_scoring_feedback**
```sql
CREATE TABLE gtm_scoring_feedback (
    id SERIAL PRIMARY KEY,
    prospect_id TEXT,
    icp_key TEXT,
    company TEXT,
    title TEXT,
    industry TEXT,
    employee_count INTEGER,
    predicted_score INTEGER,
    actual_outcome TEXT,
    feedback_source TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**gtm_state**
```sql
CREATE TABLE gtm_state (
    key TEXT PRIMARY KEY,
    value INTEGER,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Step 2: Apollo Email Sequences

Create one email sequence per ICP in Apollo. A 4-step cadence works well (Day 1, Day 3, Day 7, Day 14). Note each sequence ID from the URL.

Also note your "Send email from" account ID (found in Apollo Settings > Email Accounts).

### Step 3: HubSpot Pipeline

1. Go to Settings > Objects > Deals > Pipelines
2. Create a new pipeline called "GTM Agent Outbound"
3. Add stages: Researched, Sequenced, Engaged, Discovery Scheduled, Closed Won, Closed Lost
4. Note the pipeline ID and each stage ID (visible in the URL or via API)
5. Create custom contact properties: `icp_segment`, `research_brief`, `personalization_hook`, `recommended_angle`, `priority_score`, `lead_source`

### Step 4: Slack App

1. Create a new app at api.slack.com/apps
2. Add Bot Token Scopes: `chat:write`
3. Install to workspace, note the Bot Token (`xoxb-...`)
4. Enable Interactivity (you'll set the URL after deploying)
5. Note your Signing Secret from Basic Information
6. Create or pick a Slack channel and note the Channel ID

### Step 5: Configure Your ICP

Edit `config.py`. The repo ships with an example mobile game studio ICP. Customize for your business:

```python
ICP_CONFIGS = {
    "mobile_game_studios": {
        "name": "Mobile Game Studio Leaders",
        "emoji": "🎮",
        "product_focus": "UA analytics and creative optimization for mobile games",
        "pain_points": [
            "Rising CPIs making user acquisition unsustainable",
            "Creative fatigue across ad networks",
            "Difficulty attributing ROAS post-ATT/SKAN",
            "Scaling beyond core geo markets",
        ],
        "value_props": [
            "30% lower CPI through predictive creative testing",
            "Cross-network attribution without IDFA dependency",
            "Automated creative iteration at scale",
        ],
        "apollo_filters": {
            "person_titles": [
                "VP Marketing",
                "Head of Marketing",
                "Director Marketing",
                "VP User Acquisition",
                "Head of User Acquisition",
                "VP Growth",
                "Head of Growth",
                "VP Creative",
                "Head of Creative",
                "Chief Marketing Officer",
            ],
            "person_seniorities": ["vp", "director", "c_suite"],
            "organization_num_employees_ranges": ["20,5000"],
        },
        "target_companies": [
            "King", "Supercell", "Niantic", "Zynga",
            "Scopely", "Playtika", "Moon Active",
            # ... add 30-50 companies you want to target
        ],
    },
}
```

**Key decisions when defining your ICP:**

- **`person_titles`**: Use the format "VP [Function]", "Head of [Function]", "Director [Function]". Apollo matches as substrings, so "VP Marketing" catches "SVP Marketing" and "VP Marketing Operations" too.
- **`person_seniorities`**: Stick with `["vp", "director", "c_suite"]` unless you also want managers.
- **`organization_num_employees_ranges`**: Format is `"min,max"`. Use `"20,5000"` for mid-market, `"100,50000"` for enterprise.
- **`target_companies`**: Name 30-100 companies. The agent fuzzy-matches these. Companies not on this list can still pass through if they're in a relevant industry (configured in `apollo_client.py`).

**Multiple ICPs**: Add more keys to `ICP_CONFIGS`. Each gets its own Apollo sequence, company list, and Slack section.

### Step 6: Update Industry Filters

In `apollo_client.py`, update two sets to match your vertical:

```python
# Industries to exclude (clearly outside your market)
EXCLUDED_INDUSTRIES = {
    "consumer goods", "retail", "financial services",
    # ... add industries irrelevant to your ICP
}

# Industries that pass through even without a target company match
ADTECH_MEDIA_INDUSTRIES = {  # Rename this variable for your use case
    "computer games", "entertainment", "mobile games",
    # ... add industries your prospects operate in
}
```

### Step 7: Deploy to Railway

1. Push this repo to GitHub
2. Create a new Railway project, connect the repo
3. Create **two services** from the same repo:
   - **Cron service**: Set `START_SCRIPT=main.py`, add a cron schedule (e.g., `0 14 * * 1-5` for 2pm UTC weekdays)
   - **Actions service**: Set `START_SCRIPT=slack_actions.py`, restart policy `ON_FAILURE`
4. Add all environment variables to each service (see below)
5. Copy the Actions service public URL
6. Set Slack Interactivity Request URL to: `https://YOUR-ACTIONS-SERVICE.up.railway.app/slack/actions`

### Environment Variables

**Both services:**

| Variable | Description |
|----------|-------------|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |
| `SLACK_CHANNEL_ID` | Target Slack channel ID |
| `HUBSPOT_ACCESS_TOKEN` | HubSpot private app token |
| `APOLLO_API_KEY` | Apollo API key |
| `START_SCRIPT` | `main.py` for cron, `slack_actions.py` for actions |
| `HUBSPOT_PIPELINE_ID` | Your pipeline ID |
| `HUBSPOT_STAGE_IDS` | JSON map of stage names to IDs (see below) |

**Cron service only:**

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `COMPANY_NAME` | Your company name (used in enrichment prompts) |
| `COMPANY_DESCRIPTION_SHORT` | One-sentence description of what you sell |
| `SLACK_WEBHOOK_URL` | Slack webhook (fallback) |
| `RUN_MODE` | `production` or `dry_run` |
| `ENRICHMENT_DELAY` | Seconds between Claude calls (default: `1.0`) |

**Actions service only:**

| Variable | Description |
|----------|-------------|
| `SLACK_SIGNING_SECRET` | From Slack app Basic Information |
| `APOLLO_SEQUENCE_IDS` | JSON map of ICP key to sequence ID (see below) |
| `APOLLO_SENDER_ACCOUNT_ID` | Your Apollo email sender account ID |

**JSON env var examples:**

```
HUBSPOT_STAGE_IDS={"researched":"123456","sequenced":"123457","engaged":"123458","discovery_scheduled":"123459","closed_won":"123460","closed_lost":"123461"}

APOLLO_SEQUENCE_IDS={"mobile_game_studios":"abc123def456..."}
```

### Step 8: Test

1. Set `RUN_MODE=dry_run`
2. Trigger a manual run in Railway (or run `python main.py` locally)
3. Check logs for prospect discovery and filtering
4. Verify Slack cards appear
5. Click buttons, verify Supabase and HubSpot update
6. Switch to `RUN_MODE=production`

## How It Works (Details)

### Multi-Page Search

Each run searches 5 consecutive Apollo pages per ICP (250 raw results). Page state is tracked in Supabase so each run picks up where the last one left off. Stops early after 2 consecutive empty pages.

### Company Filtering

Two-tier post-search filter:

1. **Target match**: Fuzzy-matches prospect's company against your `target_companies` list
2. **Industry match**: Allows prospects from configured relevant industries even without a target list match

### Deduplication

Three layers:

1. **In-batch**: `first_name@company` dedup across pages within a single run
2. **Supabase**: Checks if prospect was seen in any previous run
3. **HubSpot**: Checks if email already exists as a contact

### Scoring Feedback Loop

1. Claude scores each prospect 1-10
2. "Bad Fit" clicks save to `gtm_scoring_feedback`
3. `feedback_sync.py` pulls reply/bounce data from Apollo and deal outcomes from HubSpot
4. Future enrichment prompts include this feedback so Claude adjusts scoring

## Operations

**Reset page state** (after changing filters or company lists):
```sql
UPDATE gtm_state SET value = '1' WHERE key LIKE 'apollo_page_%';
```

**Clear test prospects:**
```sql
DELETE FROM gtm_prospects WHERE status = 'discovered';
```

**Resend Slack digest:**
```bash
python resend_slack.py 2025-03-01
```

## Cost Per Run

| Service | Usage | Approximate Cost |
|---------|-------|-----------------|
| Anthropic (Claude) | ~60-180 API calls (2 per prospect) | $3-10 |
| Apollo | 1 credit per sequence enrollment | 30-90 credits |
| Railway | Cron + always-on service | ~$5/month |
| Supabase | Free tier sufficient | $0 |

## Customization

- **Volume**: Adjust `PAGES_PER_ICP` in `main.py` (default 5)
- **Quality threshold**: Prospects scoring < 6 are filtered from Slack (see `slack_notify.py`)
- **HubSpot sync**: Only prospects >= 6 with non-skip action sync to HubSpot (see `main.py`)
- **Industry passthrough**: Edit `ADTECH_MEDIA_INDUSTRIES` in `apollo_client.py` for your vertical

## License

MIT
