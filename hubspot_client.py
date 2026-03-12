"""
HubSpot client. Handles contact dedup, creation, and deal management.
"""
import os
import requests
import logging
from config import HUBSPOT_API_KEY

import json

logger = logging.getLogger(__name__)

HS_BASE = "https://api.hubapi.com"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {HUBSPOT_API_KEY}",
}

# GTM Agent Outbound pipeline (set in Railway env vars)
PIPELINE_ID = os.getenv("HUBSPOT_PIPELINE_ID", "")

# Stage IDs as JSON: {"researched": "123", "sequenced": "456", ...}
_stages_raw = os.getenv("HUBSPOT_STAGE_IDS", "{}")
try:
    STAGES = json.loads(_stages_raw)
except (json.JSONDecodeError, TypeError):
    STAGES = {}


def contact_exists(email):
    """Check if a contact already exists in HubSpot."""
    if not email:
        return False
    try:
        resp = requests.post(
            f"{HS_BASE}/crm/v3/objects/contacts/search",
            headers=HEADERS,
            json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": email,
                    }]
                }],
                "limit": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("total", 0) > 0
    except requests.RequestException as e:
        logger.error(f"HubSpot search failed: {e}")
        return False


def find_contact_by_email(email):
    """Find a contact by email. Returns contact ID or None."""
    if not email:
        return None
    try:
        resp = requests.post(
            f"{HS_BASE}/crm/v3/objects/contacts/search",
            headers=HEADERS,
            json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": email,
                    }]
                }],
                "limit": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0].get("id")
        return None
    except requests.RequestException as e:
        logger.error(f"HubSpot contact search failed: {e}")
        return None


def find_deal_by_prospect(prospect_id):
    """
    Find a deal associated with a prospect.
    Searches by deal name pattern: "{Company} - {icp_key}"
    Returns deal ID or None.
    """
    if not prospect_id:
        return None

    # Extract company from prospect_id (format: "Name@Company")
    company = prospect_id.split("@", 1)[1] if "@" in prospect_id else None
    if not company:
        return None

    try:
        resp = requests.post(
            f"{HS_BASE}/crm/v3/objects/deals/search",
            headers=HEADERS,
            json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "dealname",
                        "operator": "CONTAINS_TOKEN",
                        "value": company,
                    }]
                }],
                "limit": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0].get("id")
        return None
    except requests.RequestException as e:
        logger.error(f"HubSpot deal search failed: {e}")
        return None


def update_deal_stage(deal_id, stage_key):
    """
    Update a deal's stage. stage_key should be one of:
    researched, sequenced, engaged, discovery_scheduled,
    closed_won, closed_lost
    """
    stage_id = STAGES.get(stage_key)
    if not stage_id:
        logger.error(f"Unknown stage key: {stage_key}")
        return False

    try:
        resp = requests.patch(
            f"{HS_BASE}/crm/v3/objects/deals/{deal_id}",
            headers=HEADERS,
            json={
                "properties": {
                    "dealstage": stage_id,
                }
            },
            timeout=15,
        )
        resp.raise_for_status()
        logger.info(f"Updated deal {deal_id} to stage {stage_key}")
        return True
    except requests.RequestException as e:
        logger.error(
            f"HubSpot deal stage update failed: {e} "
            f"Response: {getattr(e.response, 'text', 'N/A') if hasattr(e, 'response') else 'N/A'}"
        )
        return False


def company_has_deals(company_name):
    """Check if any deals exist for this company."""
    if not company_name:
        return False
    try:
        resp = requests.post(
            f"{HS_BASE}/crm/v3/objects/deals/search",
            headers=HEADERS,
            json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "dealname",
                        "operator": "CONTAINS_TOKEN",
                        "value": company_name,
                    }]
                }],
                "limit": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("total", 0) > 0
    except requests.RequestException as e:
        logger.error(f"HubSpot deal search failed: {e}")
        return False


def create_contact_and_deal(prospect, enrichment, icp_key):
    """Create a HubSpot contact and deal. Returns (contact_id, deal_id)."""
    brief = "\n".join(enrichment.get("research_brief", []))

    # Build contact properties, only include email if we have one
    contact_props = {
        "firstname": prospect.get("first_name", ""),
        "lastname": prospect.get("last_name", ""),
        "jobtitle": prospect.get("title", ""),
        "company": prospect.get("company", ""),
        "icp_segment": icp_key,
        "research_brief": brief[:65535],
        "personalization_hook": enrichment.get(
            "personalization_hook", ""
        ),
        "recommended_angle": enrichment.get(
            "recommended_angle", ""
        ),
        "priority_score": str(
            enrichment.get("priority_score", "")
        ),
        "lead_source": "gtm_agent_apollo",
    }

    email = prospect.get("email")
    if email:
        contact_props["email"] = email

    try:
        contact_resp = requests.post(
            f"{HS_BASE}/crm/v3/objects/contacts",
            headers=HEADERS,
            json={"properties": contact_props},
            timeout=15,
        )
        contact_resp.raise_for_status()
        contact_id = contact_resp.json().get("id")
    except requests.RequestException as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = e.response.text[:500]
        logger.error(
            f"HubSpot contact creation failed: {e} "
            f"Response: {body}"
        )
        return None, None

    try:
        deal_resp = requests.post(
            f"{HS_BASE}/crm/v3/objects/deals",
            headers=HEADERS,
            json={
                "properties": {
                    "dealname": (
                        f"{prospect.get('company', 'Unknown')}"
                        f" - {icp_key}"
                    ),
                    "pipeline": PIPELINE_ID,
                    "dealstage": STAGES["researched"],
                    "icp_segment": icp_key,
                    "lead_source": "gtm_agent_apollo",
                    "priority_score": str(
                        enrichment.get("priority_score", "")
                    ),
                }
            },
            timeout=15,
        )
        deal_resp.raise_for_status()
        deal_id = deal_resp.json().get("id")

        # Associate deal with contact
        requests.put(
            f"{HS_BASE}/crm/v3/objects/deals/{deal_id}"
            f"/associations/contacts/{contact_id}"
            f"/deal_to_contact",
            headers=HEADERS,
            timeout=15,
        )
        return contact_id, deal_id
    except requests.RequestException as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = e.response.text[:500]
        logger.error(
            f"HubSpot deal creation failed: {e} "
            f"Response: {body}"
        )
        return contact_id, None
