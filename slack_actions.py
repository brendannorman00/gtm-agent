"""
Slack interaction handler.

Receives button clicks from the GTM Agent digest and updates
Supabase, HubSpot, and Apollo accordingly.

Deploy as a separate always-on Railway service (not a cron job).
"""
import json
import hmac
import hashlib
import time
import logging
import os
import threading

import requests
from flask import Flask, request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("slack_actions")

app = Flask(__name__)

# Environment variables
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")

# Apollo sequence IDs per ICP (set in Railway env vars as JSON or individual vars)
# Example: APOLLO_SEQUENCE_IDS={"my_icp": "abc123..."}
import ast
_seq_raw = os.getenv("APOLLO_SEQUENCE_IDS", "{}")
try:
    APOLLO_SEQUENCES = json.loads(_seq_raw)
except (json.JSONDecodeError, TypeError):
    APOLLO_SEQUENCES = {}

APOLLO_SENDER_ACCOUNT_ID = os.getenv("APOLLO_SENDER_ACCOUNT_ID", "")

# HubSpot pipeline stage IDs (set in Railway env vars as JSON)
# Example: HUBSPOT_STAGE_IDS={"researched": "123", "sequenced": "456", ...}
_stages_raw = os.getenv("HUBSPOT_STAGE_IDS", "{}")
try:
    HS_STAGES = json.loads(_stages_raw)
except (json.JSONDecodeError, TypeError):
    HS_STAGES = {}

HS_PIPELINE = os.getenv("HUBSPOT_PIPELINE_ID", "")
HS_BASE = "https://api.hubapi.com"

# Supabase client (lazy init)
_sb = None


def _get_supabase():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------

def verify_slack_signature(req):
    """Verify the request actually came from Slack."""
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not set, skipping verification")
        return True

    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")

    if not timestamp or not signature:
        return False

    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False

    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    computed = (
        "v0="
        + hmac.new(
            SLACK_SIGNING_SECRET.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(computed, signature)


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def get_prospect(prospect_id):
    """Fetch full prospect data from Supabase."""
    try:
        sb = _get_supabase()
        result = (
            sb.table("gtm_prospects")
            .select("*")
            .eq("id", prospect_id)
            .single()
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"Supabase fetch failed for {prospect_id}: {e}")
        return None


def update_prospect_status(prospect_id, new_status):
    """Update prospect status in Supabase."""
    try:
        sb = _get_supabase()
        sb.table("gtm_prospects").update({
            "status": new_status,
        }).eq("id", prospect_id).execute()
        logger.info(f"Supabase: {prospect_id} -> {new_status}")
        return True
    except Exception as e:
        logger.error(f"Supabase update failed for {prospect_id}: {e}")
        return False


def save_feedback(prospect_id, feedback_type):
    """Save feedback to gtm_scoring_feedback for the learning loop."""
    try:
        sb = _get_supabase()
        prospect = get_prospect(prospect_id)
        if not prospect:
            return

        enrichment = prospect.get("enrichment") or {}
        sb.table("gtm_scoring_feedback").upsert({
            "prospect_id": prospect_id,
            "icp_key": prospect.get("icp_key", ""),
            "company": prospect.get("company", ""),
            "title": prospect.get("name", ""),
            "predicted_score": enrichment.get("priority_score", 0),
            "actual_outcome": feedback_type,
            "feedback_source": "slack_button",
        }).execute()
        logger.info(f"Feedback saved: {prospect_id} = {feedback_type}")
    except Exception as e:
        logger.error(f"Feedback save failed: {e}")


# ---------------------------------------------------------------------------
# Apollo
# ---------------------------------------------------------------------------

def apollo_enrich_person(apollo_id):
    """Enrich a person in Apollo to get their email. Returns dict or None."""
    if not APOLLO_API_KEY or not apollo_id:
        return None
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/people/match",
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": APOLLO_API_KEY,
            },
            json={"id": apollo_id, "reveal_personal_emails": False},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(f"Apollo enrich failed: {resp.status_code}")
            return None
        return resp.json().get("person", {})
    except requests.RequestException as e:
        logger.error(f"Apollo enrich failed: {e}")
        return None


def apollo_create_contact(prospect_data, email):
    """Create a contact in Apollo. Returns contact ID or None."""
    if not APOLLO_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/contacts",
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": APOLLO_API_KEY,
            },
            json={
                "first_name": prospect_data.get("first_name", ""),
                "last_name": prospect_data.get("last_name", ""),
                "title": prospect_data.get("title", ""),
                "email": email,
                "organization_name": prospect_data.get("company", ""),
            },
            timeout=30,
        )
        resp.raise_for_status()
        contact_id = resp.json().get("contact", {}).get("id")
        logger.info(f"Apollo contact created: {contact_id}")
        return contact_id
    except requests.RequestException as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = e.response.text[:500]
        logger.error(f"Apollo contact creation failed: {e} Response: {body}")


def apollo_add_to_sequence(contact_id, sequence_id):
    """Add a contact to an Apollo sequence."""
    if not APOLLO_API_KEY or not contact_id or not sequence_id:
        return False
    try:
        resp = requests.post(
            f"https://api.apollo.io/api/v1/emailer_campaigns/{sequence_id}/add_contact_ids",
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": APOLLO_API_KEY,
            },
            json={
                "contact_ids": [contact_id],
                "emailer_campaign_id": sequence_id,
                "send_email_from_email_account_id": APOLLO_SENDER_ACCOUNT_ID,
                "sequence_active_in_other_campaigns": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(
            f"Apollo: added {contact_id} to sequence {sequence_id}"
        )
        return True
    except requests.RequestException as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = e.response.text[:500]
        logger.error(f"Apollo sequence add failed: {e} Response: {body}")


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------

def hs_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
    }


def hubspot_find_deal(company):
    """Find a deal by company name. Returns deal ID or None."""
    if not HUBSPOT_ACCESS_TOKEN or not company:
        return None
    try:
        resp = requests.post(
            f"{HS_BASE}/crm/v3/objects/deals/search",
            headers=hs_headers(),
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
        return results[0].get("id") if results else None
    except requests.RequestException as e:
        logger.error(f"HubSpot deal search failed: {e}")
        return None


def hubspot_update_deal_stage(deal_id, stage_key):
    """Update deal stage. stage_key: researched, sequenced, engaged, etc."""
    if not HUBSPOT_ACCESS_TOKEN or not deal_id:
        return False

    stage_id = HS_STAGES.get(stage_key)
    if not stage_id:
        logger.error(f"Unknown stage: {stage_key}")
        return False

    try:
        resp = requests.patch(
            f"{HS_BASE}/crm/v3/objects/deals/{deal_id}",
            headers=hs_headers(),
            json={"properties": {"dealstage": stage_id}},
            timeout=15,
        )
        resp.raise_for_status()
        logger.info(f"HubSpot deal {deal_id} -> {stage_key}")
        return True
    except requests.RequestException as e:
        logger.error(f"HubSpot deal update failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def update_message_buttons(channel, message_ts, original_blocks, status_text):
    """Replace action buttons with a status line."""
    if not SLACK_BOT_TOKEN:
        return

    updated_blocks = []
    for block in original_blocks:
        if block.get("type") == "actions":
            updated_blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"_{status_text}_",
                }],
            })
        else:
            updated_blocks.append(block)

    try:
        requests.post(
            "https://slack.com/api/chat.update",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "channel": channel,
                "ts": message_ts,
                "blocks": updated_blocks,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        logger.error(f"Slack message update failed: {e}")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def handle_sequence(prospect_id, user):
    """
    Full sequence flow:
    1. Get prospect from Supabase
    2. Enrich via Apollo to get email
    3. Create Apollo contact
    4. Add to Apollo sequence (based on ICP)
    5. Update Supabase status
    6. Update HubSpot deal stage
    """
    prospect = get_prospect(prospect_id)
    if not prospect:
        return f"Could not find prospect {prospect_id}"

    icp_key = prospect.get("icp_key", "")
    company = prospect.get("company", "")
    apollo_id = prospect.get("apollo_id")

    # 1. Enrich to get email + full name
    email = prospect.get("email")
    enriched_person = None
    if apollo_id:
        enriched_person = apollo_enrich_person(apollo_id)
        if enriched_person:
            email = enriched_person.get("email") or email
            # Update Supabase with enriched data
            try:
                sb = _get_supabase()
                sb.table("gtm_prospects").update({
                    "email": email,
                    "name": enriched_person.get("name") or prospect.get("name"),
                }).eq("id", prospect_id).execute()
            except Exception:
                pass

    if not email:
        return (
            f"No email found for {prospect_id}. "
            f"Enrich manually in Apollo first."
        )

    # 2. Create Apollo contact with enriched data
    contact_data = prospect.copy()
    if enriched_person:
        contact_data["first_name"] = enriched_person.get("first_name") or prospect.get("name", "")
        contact_data["last_name"] = enriched_person.get("last_name") or ""
    apollo_contact_id = apollo_create_contact(contact_data, email)

    # 3. Add to sequence
    sequence_id = APOLLO_SEQUENCES.get(icp_key)
    if apollo_contact_id and sequence_id:
        apollo_add_to_sequence(apollo_contact_id, sequence_id)

    # 4. Update Supabase
    update_prospect_status(prospect_id, "sequenced")

    # 5. Update HubSpot deal
    deal_id = hubspot_find_deal(company)
    if deal_id:
        hubspot_update_deal_stage(deal_id, "sequenced")

    return f"📧 {user} sequenced *{prospect_id}* via Apollo"


def handle_linkedin(prospect_id, user):
    """Mark as LinkedIn outreach."""
    prospect = get_prospect(prospect_id)
    company = prospect.get("company", "") if prospect else ""

    update_prospect_status(prospect_id, "linkedin_pending")

    deal_id = hubspot_find_deal(company)
    if deal_id:
        hubspot_update_deal_stage(deal_id, "sequenced")

    return f"💼 {user} will reach out to *{prospect_id}* on LinkedIn"


def handle_manual(prospect_id, user):
    """Mark as manually handled."""
    prospect = get_prospect(prospect_id)
    company = prospect.get("company", "") if prospect else ""

    update_prospect_status(prospect_id, "engaged")

    deal_id = hubspot_find_deal(company)
    if deal_id:
        hubspot_update_deal_stage(deal_id, "engaged")

    return f"🙋 {user} is handling *{prospect_id}* personally"


def handle_bad_fit(prospect_id, user):
    """Mark as bad fit and save feedback."""
    update_prospect_status(prospect_id, "bad_fit")
    save_feedback(prospect_id, "bad_fit")

    return (
        f"👎 {user} marked *{prospect_id}* as bad fit "
        f"(agent will learn)"
    )


def handle_view_linkedin(prospect_id, user):
    """Log LinkedIn profile view. URL button opens the link directly."""
    return f"🔗 {user} viewed LinkedIn for *{prospect_id}*"


ACTION_HANDLERS = {
    "prospect_sequence": handle_sequence,
    "prospect_linkedin": handle_linkedin,
    "prospect_manual": handle_manual,
    "prospect_skip": handle_bad_fit,
    "prospect_view_linkedin": handle_view_linkedin,
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/slack/actions", methods=["POST"])
def handle_action():
    """Handle button clicks from Slack prospect cards."""

    if not verify_slack_signature(request):
        logger.warning("Invalid Slack signature")
        return jsonify({"error": "Invalid signature"}), 403

    payload = json.loads(request.form.get("payload", "{}"))

    actions = payload.get("actions", [])
    if not actions:
        return jsonify({"ok": True})

    action = actions[0]
    action_id = action.get("action_id", "")
    prospect_id = action.get("value", "")
    user = payload.get("user", {}).get("real_name", "Someone")
    channel = payload.get("channel", {}).get("id", "")
    message_ts = payload.get("message", {}).get("ts", "")
    original_blocks = payload.get("message", {}).get("blocks", [])

    handler = ACTION_HANDLERS.get(action_id)
    if not handler:
        logger.warning(f"Unknown action_id: {action_id}")
        return jsonify({"ok": True})

    logger.info(
        f"Action: {action_id} | Prospect: {prospect_id} | User: {user}"
    )

    # Process in background thread so Slack gets a 200 within 3s
    def process_action():
        status_text = handler(prospect_id, user)
        update_message_buttons(
            channel, message_ts, original_blocks, status_text
        )

    threading.Thread(target=process_action, daemon=True).start()

    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "gtm-slack-actions"})


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "service": "gtm-slack-actions"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    logger.info(f"Starting Slack action handler on port {port}")
    app.run(host="0.0.0.0", port=port)
