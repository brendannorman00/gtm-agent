"""
Claude-powered prospect enrichment and decision engine.
"""
import anthropic
import json
import time
import logging
from config import ANTHROPIC_API_KEY, ENRICHMENT_DELAY

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ENRICHMENT_PROMPT = """You are a sales research assistant for {company_name}. Your company's focus: {company_description_short}

## Prospect
Name: {name}
Title: {title}
Company: {company}
Company Description: {company_description}
Industry: {industry}
Employee Count: {employee_count}
Technologies: {technologies}
Funding: {funding_info}
Location: {location}

## ICP Context
Segment: {icp_name}
Product Focus: {product_focus}
Pain Points:
{pain_points}
Value Props:
{value_props}

## Historical Performance Data
{feedback_context}

## Instructions
Generate a sales research brief. Return ONLY valid JSON with these fields:

{{
  "research_brief": ["bullet 1", "bullet 2", "bullet 3"],
  "personalization_hook": "1-2 sentence cold email opener referencing something specific",
  "recommended_angle": "One sentence on which product to lead with and why",
  "objection_prediction": "Most likely objection and a realistic counter",
  "priority_score": 7,
  "priority_reasoning": "Brief explanation of the score"
}}

Be specific, not generic. Reference real signals from the prospect data. No markdown, no backticks, no preamble."""

DECISION_PROMPT = """You are the GTM strategist for {company_name}. Recommend the best outreach approach for this prospect.

## Prospect
Name: {name} | Title: {title} | Company: {company}
Priority Score: {priority_score}/10
Research Brief: {research_brief}
Personalization Hook: {personalization_hook}

## Context
Warm paths available: {warm_paths}
Already in HubSpot: {in_hubspot}
Company previously contacted: {company_contacted}
Visited website: {site_visitor}

## Decision
Return ONLY valid JSON:
{{
  "action": "warm_intro" | "cold_sequence" | "linkedin_first" | "skip" | "wait",
  "urgency": "now" | "this_week" | "next_week" | "backlog",
  "reasoning": "2 sentences max",
  "custom_notes": "Anything the CEO should know before reaching out"
}}"""


def _get_feedback_context(icp_key):
    """
    Pull scoring feedback patterns from Supabase.
    Returns a string summary for the enrichment prompt.
    """
    try:
        from supabase import create_client
        from config import SUPABASE_URL, SUPABASE_KEY
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Get outcomes for this ICP
        result = (
            sb.table("gtm_scoring_feedback")
            .select("*")
            .eq("icp_key", icp_key)
            .execute()
        )
        rows = result.data or []

        if len(rows) < 5:
            return "Not enough historical data yet. Score based on ICP fit."

        # Analyze patterns
        replied = [r for r in rows if r["actual_outcome"] == "replied"]
        converted = [
            r for r in rows if r["actual_outcome"] == "converted"
        ]
        bad_fit = [
            r for r in rows if r["actual_outcome"] == "bad_fit"
        ]

        lines = []
        lines.append(
            f"Historical data: {len(rows)} prospects scored in "
            f"this ICP."
        )
        lines.append(
            f"Reply rate: {len(replied)}/{len(rows)} "
            f"({100 * len(replied) // len(rows)}%)"
        )

        if converted:
            titles = [r.get("title", "") for r in converted]
            companies = [r.get("company", "") for r in converted]
            lines.append(
                f"Converted prospects had titles like: "
                f"{', '.join(t for t in titles[:3] if t)}"
            )
            lines.append(
                f"Converted from companies like: "
                f"{', '.join(c for c in companies[:3] if c)}"
            )

        if replied:
            avg_score = sum(
                r.get("predicted_score", 0) for r in replied
            ) // len(replied)
            lines.append(
                f"Prospects who replied had avg predicted score: "
                f"{avg_score}"
            )

        if bad_fit:
            bad_companies = [
                r.get("company", "") for r in bad_fit[:5]
            ]
            bad_industries = list(set(
                r.get("industry", "")
                for r in bad_fit
                if r.get("industry")
            ))
            lines.append(
                f"Bad fit companies included: "
                f"{', '.join(c for c in bad_companies if c)}"
            )
            if bad_industries:
                lines.append(
                    f"Industries that don't convert: "
                    f"{', '.join(bad_industries[:3])}"
                )

        lines.append(
            "Adjust your scoring to reflect these patterns. "
            "Score DOWN prospects similar to bad fits. "
            "Score UP prospects similar to converters."
        )

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Failed to get feedback context: {e}")
        return "No historical data available."


def enrich_prospect(prospect, icp_config, feedback_context=""):
    """Enrich a single prospect using Claude."""
    from config import COMPANY_NAME, COMPANY_DESCRIPTION_SHORT
    location = prospect.get("location", "N/A")

    prompt = ENRICHMENT_PROMPT.format(
        company_name=COMPANY_NAME,
        company_description_short=COMPANY_DESCRIPTION_SHORT,
        name=prospect.get("name", "Unknown"),
        title=prospect.get("title", "Unknown"),
        company=prospect.get("company", "Unknown"),
        company_description=prospect.get("company_description", "N/A"),
        industry=prospect.get("industry", "N/A"),
        employee_count=prospect.get("employee_count", "N/A"),
        technologies=", ".join(prospect.get("technologies", [])) or "N/A",
        funding_info=prospect.get("funding_info", "N/A"),
        location=location,
        icp_name=icp_config["name"],
        product_focus=icp_config["product_focus"],
        pain_points="\n".join(f"- {p}" for p in icp_config["pain_points"]),
        value_props="\n".join(f"- {v}" for v in icp_config["value_props"]),
        feedback_context=feedback_context,
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1000,
            timeout=60.0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error for {prospect['name']}: {e}")
        return None
    except Exception as e:
        logger.error(f"Enrichment failed for {prospect['name']}: {e}")
        return None


def decide_action(prospect, enrichment, context):
    """Use Claude to decide the best outreach approach."""
    from config import COMPANY_NAME
    brief = "\n".join(enrichment.get("research_brief", []))

    prompt = DECISION_PROMPT.format(
        company_name=COMPANY_NAME,
        name=prospect.get("name"),
        title=prospect.get("title"),
        company=prospect.get("company"),
        priority_score=enrichment.get("priority_score", "N/A"),
        research_brief=brief,
        personalization_hook=enrichment.get("personalization_hook", ""),
        warm_paths=context.get("warm_paths", "None identified"),
        in_hubspot=context.get("in_hubspot", False),
        company_contacted=context.get("company_contacted", False),
        site_visitor=context.get("site_visitor", False),
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            timeout=60.0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        logger.error(f"Decision failed for {prospect['name']}: {e}")
        score = enrichment.get("priority_score", 0)
        if score >= 8:
            return {"action": "cold_sequence", "urgency": "this_week",
                    "reasoning": "High score, defaulting to cold sequence",
                    "custom_notes": ""}
        elif score >= 6:
            return {"action": "cold_sequence", "urgency": "next_week",
                    "reasoning": "Medium score", "custom_notes": ""}
        else:
            return {"action": "skip", "urgency": "backlog",
                    "reasoning": "Low score", "custom_notes": ""}


def batch_enrich(prospects, icp_config, icp_key=""):
    """Enrich a list of prospects. Returns list of result dicts."""
    feedback_context = _get_feedback_context(icp_key) if icp_key else ""
    results = []
    for i, prospect in enumerate(prospects):
        logger.info(
            f"  Enriching {i + 1}/{len(prospects)}: "
            f"{prospect['name']} @ {prospect['company']}"
        )

        enrichment = enrich_prospect(prospect, icp_config, feedback_context)
        if not enrichment:
            continue

        context = {
            "warm_paths": "None identified",
            "in_hubspot": False,
            "company_contacted": False,
            "site_visitor": False,
        }

        decision = decide_action(prospect, enrichment, context)

        results.append({
            "prospect": prospect,
            "enrichment": enrichment,
            "decision": decision,
        })

        time.sleep(ENRICHMENT_DELAY)

    return results
