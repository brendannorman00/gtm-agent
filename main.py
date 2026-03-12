#!/usr/bin/env python3
"""
GTM Agent - Daily Orchestrator

Runs on a schedule via Railway cron. Discovers prospects via Apollo,
enriches with Claude, deduplicates against HubSpot, and sends a
ranked Slack digest.
"""
import logging
import sys
import time
from datetime import datetime

from config import ICP_CONFIGS, MAX_PROSPECTS_PER_ICP, RUN_MODE
from apollo_client import search_prospects
from enrichment import batch_enrich
from hubspot_client import contact_exists
from hubspot_client import create_contact_and_deal
from slack_notify import send_digest
from state import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gtm_agent")

# Number of Apollo pages to search per ICP per run.
# Each page returns up to 50 results, post-filtered to target companies.
# 5 pages = 250 raw results, typically yielding 10-30 qualified prospects.
PAGES_PER_ICP = 5


def run_pipeline():
    """Main pipeline orchestrator."""
    dry_run = RUN_MODE == "dry_run"
    start_time = time.time()

    logger.info("=" * 60)
    logger.info("GTM Agent starting")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'PRODUCTION'}")
    logger.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    state = StateStore()
    results_by_icp = {}

    for icp_key, icp_config in ICP_CONFIGS.items():
        logger.info(f"\n--- ICP: {icp_config['name']} ---")

        start_page = state.get_apollo_page(icp_key)
        existing_count = state.get_icp_count(icp_key)
        logger.info(
            f"  {existing_count} prospects in DB for {icp_key}, "
            f"starting at page {start_page}"
        )

        # Step 1: Search Apollo across multiple pages
        logger.info(
            f"Step 1: Searching Apollo ({PAGES_PER_ICP} pages)..."
        )
        all_raw_prospects = []
        empty_pages = 0
        last_page_searched = start_page

        for page_offset in range(PAGES_PER_ICP):
            page = start_page + page_offset
            last_page_searched = page
            raw = search_prospects(
                icp_key, icp_config,
                page=page,
                per_page=50,
            )
            if raw:
                all_raw_prospects.extend(raw)
                logger.info(
                    f"  Page {page}: {len(raw)} qualified prospects"
                )
                empty_pages = 0
            else:
                empty_pages += 1
                logger.info(f"  Page {page}: 0 qualified prospects")
                # Stop if 2 consecutive empty pages (unlikely to find
                # more target companies deeper in results)
                if empty_pages >= 2:
                    logger.info(
                        f"  Stopping early: {empty_pages} consecutive "
                        f"empty pages"
                    )
                    break

        logger.info(
            f"  Searched pages {start_page}-{last_page_searched}, "
            f"found {len(all_raw_prospects)} total prospects"
        )

        if not all_raw_prospects:
            logger.warning(
                f"No prospects found for {icp_key} across "
                f"{last_page_searched - start_page + 1} pages, "
                f"resetting to page 1"
            )
            state.set_apollo_page(icp_key, 1)
            results_by_icp[icp_key] = []
            continue

        # Advance page past last searched page for next run
        state.set_apollo_page(icp_key, last_page_searched + 1)

        # Step 2: Deduplicate
        logger.info("Step 2: Deduplicating...")
        new_prospects = []
        seen_in_batch = set()
        for p in all_raw_prospects:
            # Deduplicate within this batch (cross-page duplicates)
            batch_key = (
                f"{p.get('first_name', '')}@{p.get('company', '')}"
            )
            if batch_key in seen_in_batch:
                continue
            seen_in_batch.add(batch_key)

            if state.has_been_seen(p):
                continue
            if p.get("email") and contact_exists(p["email"]):
                logger.info(
                    f"  Skip {p['name']} (already in HubSpot)"
                )
                continue
            if state.company_contacted(p["company"]):
                logger.info(
                    f"  Skip {p['name']} (company contacted)"
                )
                continue
            new_prospects.append(p)

        logger.info(
            f"  {len(all_raw_prospects)} raw -> "
            f"{len(new_prospects)} new after dedup"
        )

        if not new_prospects:
            results_by_icp[icp_key] = []
            continue

        # Step 3: Enrich with Claude
        logger.info(
            f"Step 3: Enriching {len(new_prospects)} prospects..."
        )
        enriched = batch_enrich(new_prospects, icp_config)
        logger.info(f"  Enriched {len(enriched)} successfully")

        # Step 4: Save state
        logger.info("Step 4: Saving to state store...")
        for result in enriched:
            state.save_prospect(
                result["prospect"],
                result["enrichment"],
                result["decision"],
                icp_key,
            )

        # Step 5: Sync to HubSpot
        if not dry_run:
            logger.info("Step 5: Syncing to HubSpot...")
            for result in enriched:
                score = result["enrichment"].get("priority_score", 0)
                action = result["decision"].get("action", "skip")
                if score >= 6 and action != "skip":
                    cid, did = create_contact_and_deal(
                        result["prospect"],
                        result["enrichment"],
                        icp_key,
                    )
                    if cid:
                        logger.info(
                            f"  Created HubSpot records for "
                            f"{result['prospect']['name']}"
                        )
        else:
            logger.info("Step 5: Skipping HubSpot (dry run)")

        results_by_icp[icp_key] = enriched

    # Step 6: Slack digest
    pipeline_stats = state.get_pipeline_stats()
    logger.info("\nStep 6: Sending Slack digest...")
    send_digest(results_by_icp, pipeline_stats)

    elapsed = time.time() - start_time
    total = sum(len(r) for r in results_by_icp.values())
    logger.info(
        f"\nDone in {elapsed:.1f}s. "
        f"Processed {total} prospects."
    )


if __name__ == "__main__":
    run_pipeline()
    # Step 7: Sync feedback from previous runs
    from feedback_sync import run_feedback_sync
    logger.info("\nStep 7: Syncing feedback from Apollo + HubSpot...")
    run_feedback_sync()
