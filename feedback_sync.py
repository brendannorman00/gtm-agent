"""
Feedback sync. Pulls engagement signals from Apollo and HubSpot
back into Supabase so the scoring model can learn.
"""
import requests
import logging
from datetime import datetime, timezone
from config import APOLLO_API_KEY, HUBSPOT_API_KEY, SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

try:
    from supabase import create_client
    sb = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None
except ImportError:
    sb = None

APOLLO_BASE = "https://api.apollo.io/api/v1"
APOLLO_HEADERS = {
    "Content-Type": "application/json",
    "X-Api-Key": APOLLO_API_KEY,
}

HS_BASE = "https://api.hubapi.com"
HS_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {HUBSPOT_API_KEY}",
}


def sync_apollo_engagement():
    """
    Pull email engagement data from Apollo sequences.
    Updates: email_opened, email_replied, email_clicked, email_bounced
    """
    if not sb:
        return

    # Get all prospects in "sequenced" status
    try:
        result = (
            sb.table("gtm_prospects")
            .select("id, email, apollo_id")
            .eq("status", "sequenced")
            .execute()
        )
        prospects = result.data or []
    except Exception as e:
        logger.error(f"Failed to fetch sequenced prospects: {e}")
        return

    logger.info(f"Checking Apollo engagement for {len(prospects)} prospects")

    for prospect in prospects:
        if not prospect.get("email"):
            continue

        try:
            resp = requests.get(
                f"{APOLLO_BASE}/contacts/search",
                headers=APOLLO_HEADERS,
                params={"q_keywords": prospect["email"]},
                timeout=15,
            )
            if resp.status_code != 200:
                continue

            contacts = resp.json().get("contacts", [])
            if not contacts:
                continue

            contact = contacts[0]
            contact_id = contact.get("id")

            # Check email status from contact record
            email_status = contact.get("contact_campaign_statuses", [])

            opened = any(
                s.get("emails_opened", 0) > 0
                for s in email_status
            )
            replied = any(
                s.get("emails_replied", 0) > 0
                for s in email_status
            )
            clicked = any(
                s.get("emails_clicked", 0) > 0
                for s in email_status
            )
            bounced = any(
                s.get("emails_bounced", 0) > 0
                for s in email_status
            )

            updates = {
                "email_opened": opened,
                "email_replied": replied,
                "email_clicked": clicked,
                "email_bounced": bounced,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            # Determine outcome
            if replied:
                updates["outcome"] = "replied"
                updates["status"] = "engaged"
            elif bounced:
                updates["outcome"] = "bounced"
                updates["status"] = "bad_fit"

            sb.table("gtm_prospects").update(
                updates
            ).eq("id", prospect["id"]).execute()

            if replied:
                logger.info(f"Reply detected: {prospect['id']}")

                # Log to scoring feedback
                sb.table("gtm_scoring_feedback").insert({
                    "prospect_id": prospect["id"],
                    "icp_key": prospect.get("icp_key"),
                    "predicted_score": prospect.get("priority_score"),
                    "actual_outcome": "replied",
                    "company": prospect.get("company"),
                    "title": prospect.get("title"),
                }).execute()

        except Exception as e:
            logger.error(
                f"Apollo engagement check failed for "
                f"{prospect['id']}: {e}"
            )


def sync_hubspot_deals():
    """
    Pull deal stage updates from HubSpot.
    Updates: deal_stage, deal_stage_updated_at, outcome
    """
    if not sb:
        return

    # Get all prospects that have been synced to HubSpot
    try:
        result = (
            sb.table("gtm_prospects")
            .select("id, email, company")
            .in_("status", [
                "discovered", "sequenced", "engaged", "meeting"
            ])
            .execute()
        )
        prospects = result.data or []
    except Exception as e:
        logger.error(f"Failed to fetch prospects for HubSpot sync: {e}")
        return

    logger.info(
        f"Checking HubSpot deal stages for {len(prospects)} prospects"
    )

    for prospect in prospects:
        if not prospect.get("email"):
            continue

        try:
            # Search for contact by email
            resp = requests.post(
                f"{HS_BASE}/crm/v3/objects/contacts/search",
                headers=HS_HEADERS,
                json={
                    "filterGroups": [{
                        "filters": [{
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": prospect["email"],
                        }]
                    }],
                    "limit": 1,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                continue

            contacts = resp.json().get("results", [])
            if not contacts:
                continue

            contact_id = contacts[0]["id"]

            # Get associated deals
            assoc_resp = requests.get(
                f"{HS_BASE}/crm/v3/objects/contacts/{contact_id}"
                f"/associations/deals",
                headers=HS_HEADERS,
                timeout=15,
            )
            if assoc_resp.status_code != 200:
                continue

            deal_ids = [
                r["id"]
                for r in assoc_resp.json().get("results", [])
            ]

            if not deal_ids:
                continue

            # Get deal details
            deal_resp = requests.get(
                f"{HS_BASE}/crm/v3/objects/deals/{deal_ids[0]}",
                headers=HS_HEADERS,
                params={
                    "properties": "dealstage,closedate,amount"
                },
                timeout=15,
            )
            if deal_resp.status_code != 200:
                continue

            deal = deal_resp.json()
            stage = deal.get("properties", {}).get("dealstage", "")

            # Map HubSpot stages to outcomes
            stage_to_status = {
                "appointmentscheduled": "meeting",
                "qualifiedtobuy": "engaged",
                "closedwon": "converted",
                "closedlost": "lost",
            }

            new_status = stage_to_status.get(stage)
            if new_status:
                sb.table("gtm_prospects").update({
                    "deal_stage": stage,
                    "deal_stage_updated_at":
                        datetime.now(timezone.utc).isoformat(),
                    "status": new_status,
                    "outcome": (
                        "converted" if new_status == "converted"
                        else "lost" if new_status == "lost"
                        else None
                    ),
                    "updated_at":
                        datetime.now(timezone.utc).isoformat(),
                }).eq("id", prospect["id"]).execute()

                if new_status in ("converted", "lost"):
                    # Log final outcome to scoring feedback
                    orig = (
                        sb.table("gtm_prospects")
                        .select(
                            "priority_score, icp_key, "
                            "company, industry, employee_count, "
                            "enrichment"
                        )
                        .eq("id", prospect["id"])
                        .execute()
                    )
                    if orig.data:
                        d = orig.data[0]
                        sb.table("gtm_scoring_feedback").insert({
                            "prospect_id": prospect["id"],
                            "icp_key": d.get("icp_key"),
                            "predicted_score": d.get("priority_score"),
                            "actual_outcome": new_status,
                            "company": d.get("company"),
                            "industry": d.get("industry"),
                            "employee_count": d.get("employee_count"),
                            "title": d.get("title"),
                        }).execute()

                    logger.info(
                        f"Deal outcome for {prospect['id']}: "
                        f"{new_status}"
                    )

        except Exception as e:
            logger.error(
                f"HubSpot deal check failed for "
                f"{prospect['id']}: {e}"
            )


def run_feedback_sync():
    """Run all feedback syncs."""
    logger.info("Starting feedback sync...")
    sync_apollo_engagement()
    sync_hubspot_deals()
    logger.info("Feedback sync complete")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_feedback_sync()
