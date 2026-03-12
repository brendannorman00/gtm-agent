"""
Slack digest notification.
Sends each prospect as an individual message with action buttons
so you can approve/skip/rank each one independently.
"""
import requests
import logging
from config import SLACK_WEBHOOK_URL, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, RUN_MODE

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api/chat.postMessage"


def _post_message(blocks, text="GTM Agent"):
    """Post a single message to Slack via Bot API."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        logger.error(
            "SLACK_BOT_TOKEN or SLACK_CHANNEL_ID not set. "
            "Falling back to webhook."
        )
        return _post_webhook(blocks, text)

    try:
        resp = requests.post(
            SLACK_API,
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "channel": SLACK_CHANNEL_ID,
                "blocks": blocks,
                "text": text,
            },
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error(f"Slack API error: {data.get('error')}")
            return None
        return data.get("ts")
    except requests.RequestException as e:
        logger.error(f"Slack post failed: {e}")
        return None


def _post_webhook(blocks, text="GTM Agent"):
    """Fallback: post via webhook (single message, no buttons)."""
    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            json={"blocks": blocks, "text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Slack webhook failed: {e}")
        return None


def send_digest(results_by_icp, pipeline_stats):
    """Send the daily GTM digest to Slack as individual messages."""
    if RUN_MODE == "dry_run":
        logger.info("DRY RUN: Would send Slack digest")
        _print_digest(results_by_icp, pipeline_stats)
        return

    # 1. Send header message
    header_blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🎯 GTM Agent Daily Brief",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f"Pipeline: *{pipeline_stats.get('researched', 0)}* "
                    f"researched | *{pipeline_stats.get('sequenced', 0)}* "
                    f"sequenced | *{pipeline_stats.get('engaged', 0)}* "
                    f"engaged | *{pipeline_stats.get('discovery', 0)}* "
                    f"discovery"
                ),
            }],
        },
    ]
    _post_message(header_blocks, "GTM Agent Daily Brief")

    # 2. Send each ICP section
    icp_labels = {
        "publisher_revenue": ("📰", "PUBLISHER REVENUE"),
        "ssp_product": ("🔄", "SSP / EXCHANGE"),
        "agency_brand": ("🏢", "AGENCY / BRAND"),
    }

    action_emojis = {
        "warm_intro": "🤝",
        "cold_sequence": "📧",
        "linkedin_first": "💼",
        "skip": "⏭️",
        "wait": "⏳",
    }

    urgency_labels = {
        "now": "🔴 NOW",
        "this_week": "🟡 This week",
        "next_week": "Next week",
        "backlog": "Backlog",
    }

    total_sent = 0

    for icp_key, results in results_by_icp.items():
        if not results:
            continue

        results.sort(
            key=lambda r: r["enrichment"].get("priority_score", 0),
            reverse=True,
        )
        top_results = [
            r for r in results
            if r["enrichment"].get("priority_score", 0) >= 6
        ]

        if not top_results:
            continue

        emoji, label = icp_labels.get(icp_key, ("📋", icp_key.upper()))

        # Send ICP section header
        _post_message(
            [{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} *{label}* "
                        f"({len(top_results)} new prospects)"
                    ),
                },
            },
            {"type": "divider"}],
            f"{label} prospects",
        )

        # 3. Send each prospect as its own message
        for i, result in enumerate(top_results[:10]):
            p = result["prospect"]
            e = result["enrichment"]
            d = result["decision"]

            score = e.get("priority_score", "?")
            first = p.get("first_name", "")
            last = p.get("last_name", "")
            name = f"{first} {last}".strip() or p.get("name", "Unknown")

            # Prospect ID for button callbacks
            prospect_id = (
                p.get("email")
                or f"{first}@{p.get('company', 'Unknown')}"
            )

            # Score emoji
            if isinstance(score, (int, float)) or str(score).isdigit():
                s = int(score)
                score_bar = "🟢" if s >= 8 else "🟡" if s >= 6 else "🔴"
            else:
                score_bar = "⚪"
                s = 0

            a_emoji = action_emojis.get(d.get("action"), "📋")
            u_label = urgency_labels.get(
                d.get("urgency"), d.get("urgency", "")
            )

            action_display = d.get("action", "N/A").replace("_", " ").title()

            # LinkedIn URL if available
            linkedin = p.get("linkedin_url") or ""

            # Build action buttons
            action_buttons = []

            # Conditional LinkedIn button (only if URL exists)
            if linkedin:
                action_buttons.append({
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🔗 View LinkedIn",
                        "emoji": True,
                    },
                    "url": linkedin,
                    "action_id": "prospect_view_linkedin",
                    "value": prospect_id,
                })

            action_buttons.extend([
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "📧 Sequence",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": "prospect_sequence",
                    "value": prospect_id,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "💼 LinkedIn Outreach",
                        "emoji": True,
                    },
                    "action_id": "prospect_linkedin",
                    "value": prospect_id,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "👎 Bad Fit",
                        "emoji": True,
                    },
                    "style": "danger",
                    "action_id": "prospect_skip",
                    "value": prospect_id,
                },
            ])

            # Build prospect card blocks
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{score_bar} *{name}* | "
                            f"{p.get('title', 'Unknown')} @ "
                            f"*{p.get('company', 'Unknown')}*\n"
                            f"Score: *{score}/10* | "
                            f"{a_emoji} {action_display} | "
                            f"{u_label}"
                        ),
                    },
                },
                {
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": (
                            f"💡 _{e.get('personalization_hook', 'No hook')}_"
                            f"\n📐 _{e.get('recommended_angle', d.get('reasoning', ''))}_"
                        ),
                    }],
                },
                {
                    "type": "actions",
                    "elements": action_buttons,
                },
                {"type": "divider"},
            ]

            ts = _post_message(blocks, f"Prospect: {name}")
            if ts:
                total_sent += 1

    # 4. Send footer
    total_new = sum(len(r) for r in results_by_icp.values())
    total_actionable = sum(
        1 for results in results_by_icp.values()
        for r in results
        if r["decision"].get("action") in (
            "warm_intro", "cold_sequence", "linkedin_first"
        )
        and r["enrichment"].get("priority_score", 0) >= 6
    )

    _post_message(
        [{
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f"Discovered *{total_new}* new prospects | "
                    f"*{total_actionable}* actionable | "
                    f"*{total_sent}* cards sent"
                ),
            }],
        }],
        "GTM Agent summary",
    )

    logger.info(
        f"Slack digest sent: {total_sent} prospect cards across "
        f"{len(results_by_icp)} ICPs"
    )


def _print_digest(results_by_icp, pipeline_stats):
    """Fallback: print digest to stdout (visible in Railway logs)."""
    print("\n" + "=" * 60)
    print("🎯 GTM Agent Daily Brief")
    print("=" * 60)

    for icp_key, results in results_by_icp.items():
        results.sort(
            key=lambda r: r["enrichment"].get("priority_score", 0),
            reverse=True,
        )
        top = [
            r for r in results
            if r["enrichment"].get("priority_score", 0) >= 6
        ]
        if not top:
            continue

        print(f"\n{icp_key.upper()} ({len(top)} new)")
        print("-" * 40)

        for i, result in enumerate(top[:10]):
            p = result["prospect"]
            e = result["enrichment"]
            d = result["decision"]
            first = p.get("first_name", "")
            last = p.get("last_name", "")
            name = f"{first} {last}".strip() or p.get("name", "Unknown")
            print(
                f"\n  {i + 1}. {name}, {p.get('title', 'Unknown')} "
                f"@ {p.get('company', 'Unknown')} "
                f"[Score: {e.get('priority_score', '?')}/10]"
            )
            print(
                f"     Hook: "
                f"{e.get('personalization_hook', 'N/A')}"
            )
            print(
                f"     Action: {d.get('action', 'N/A')} | "
                f"Urgency: {d.get('urgency', 'N/A')}"
            )
            print(f"     Why: {d.get('reasoning', 'N/A')}")

    print("\n" + "=" * 60)
