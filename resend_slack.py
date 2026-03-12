# resend_slack.py
"""
Resend Slack digest for prospects created after a given date.
Usage: python resend_slack.py [YYYY-MM-DD]
Defaults to today if no date provided.
"""
import sys
from datetime import datetime, timezone
from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY
from slack_notify import send_digest

# Accept date argument or default to today
if len(sys.argv) > 1:
    since_date = sys.argv[1] + "T00:00:00Z"
else:
    since_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

rows = sb.table("gtm_prospects") \
    .select("*") \
    .gte("created_at", since_date) \
    .execute().data

results_by_icp = {}
for row in rows:
    icp = row.get("icp_key", "agency_brand")
    enrichment = row.get("enrichment") or {}
    decision = row.get("decision") or {}
    name = row.get("name", "Unknown")
    parts = name.split(" ", 1)

    prospect = {
        "name": name,
        "first_name": parts[0],
        "last_name": parts[1] if len(parts) > 1 else "",
        "title": enrichment.get("title", ""),
        "company": row.get("company", "Unknown"),
        "email": row.get("email", ""),
        "linkedin_url": enrichment.get("linkedin_url", ""),
    }

    entry = {
        "prospect": prospect,
        "enrichment": enrichment,
        "decision": decision,
    }
    results_by_icp.setdefault(icp, []).append(entry)

stats = {}
for key, status in [("researched", "discovered"), ("sequenced", "sequenced"), ("engaged", "engaged"), ("discovery", "meeting")]:
    resp = sb.table("gtm_prospects").select("id", count="exact").eq("status", status).execute()
    stats[key] = resp.count or 0

print(f"Resending {sum(len(v) for v in results_by_icp.values())} prospects across {len(results_by_icp)} ICPs")
send_digest(results_by_icp, stats)
