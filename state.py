"""
Supabase state store. Tracks prospects across runs to avoid duplicates.
Falls back to in-memory if Supabase isn't configured.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    from supabase import create_client
    from config import SUPABASE_URL, SUPABASE_KEY
    HAS_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)
except ImportError:
    HAS_SUPABASE = False


class StateStore:
    def __init__(self):
        if HAS_SUPABASE:
            self.client = create_client(SUPABASE_URL, SUPABASE_KEY)
            self.mode = "supabase"
            logger.info("State store: Supabase")
        else:
            self.seen = set()
            self.mode = "memory"
            logger.info("State store: in-memory (no persistence)")

    def has_been_seen(self, prospect):
        """Check if we've already processed this prospect."""
        key = (
            prospect.get("email")
            or f"{prospect['name']}@{prospect['company']}"
        )
        if self.mode == "supabase":
            try:
                result = (
                    self.client.table("gtm_prospects")
                    .select("id")
                    .eq("id", key)
                    .execute()
                )
                return len(result.data) > 0
            except Exception as e:
                logger.error(f"Supabase lookup failed: {e}")
                return False
        else:
            return key in self.seen

    def get_icp_count(self, icp_key):
        """Get number of prospects already stored for this ICP."""
        if self.mode == "supabase":
            try:
                result = (
                    self.client.table("gtm_prospects")
                    .select("id", count="exact")
                    .eq("icp_key", icp_key)
                    .execute()
                )
                return result.count or 0
            except Exception as e:
                logger.error(f"Supabase count failed: {e}")
        return 0

    def get_apollo_page(self, icp_key):
        """
        Get the current Apollo search page for this ICP.
        Returns 1 if no page has been stored yet.
        """
        if self.mode == "supabase":
            try:
                result = (
                    self.client.table("gtm_state")
                    .select("value")
                    .eq("key", f"apollo_page_{icp_key}")
                    .execute()
                )
                if result.data:
                    return result.data[0]["value"]
            except Exception as e:
                logger.error(f"Supabase get_apollo_page failed: {e}")
        return 1

    def set_apollo_page(self, icp_key, page):
        """
        Persist the next Apollo search page for this ICP.
        Resets to 1 if page is set to 1 (used after empty results).
        """
        if self.mode == "supabase":
            try:
                self.client.table("gtm_state").upsert({
                    "key": f"apollo_page_{icp_key}",
                    "value": page,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
                logger.info(f"Apollo page for {icp_key} set to {page}")
            except Exception as e:
                logger.error(f"Supabase set_apollo_page failed: {e}")

    def company_contacted(self, company_name):
        """Check if anyone at this company has been contacted."""
        if self.mode == "supabase":
            try:
                result = (
                    self.client.table("gtm_prospects")
                    .select("id")
                    .eq("company", company_name)
                    .in_("status", ["sequenced", "engaged", "meeting"])
                    .execute()
                )
                return len(result.data) > 0
            except Exception as e:
                logger.error(f"Supabase company check failed: {e}")
                return False
        return False

    def save_prospect(self, prospect, enrichment, decision, icp_key):
        """Save a processed prospect."""
        key = (
            prospect.get("email")
            or f"{prospect['name']}@{prospect['company']}"
        )
        if self.mode == "supabase":
            try:
                self.client.table("gtm_prospects").upsert({
                    "id": key,
                    "apollo_id": prospect.get("apollo_id"),
                    "email": prospect.get("email"),
                    "name": prospect.get("name"),
                    "company": prospect.get("company"),
                    "linkedin_url": prospect.get("linkedin_url"),
                    "icp_key": icp_key,
                    "priority_score": enrichment.get("priority_score"),
                    "status": "discovered",
                    "enrichment": enrichment,
                    "decision": decision,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
            except Exception as e:
                logger.error(f"Supabase save failed: {e}")
        else:
            self.seen.add(key)

    def get_pipeline_stats(self):
        """Get current pipeline stage counts."""
        if self.mode == "supabase":
            try:
                stats = {}
                for status in [
                    "discovered", "sequenced", "engaged", "meeting"
                ]:
                    result = (
                        self.client.table("gtm_prospects")
                        .select("id", count="exact")
                        .eq("status", status)
                        .execute()
                    )
                    stats[status] = result.count or 0
                return {
                    "researched": stats.get("discovered", 0),
                    "sequenced": stats.get("sequenced", 0),
                    "engaged": stats.get("engaged", 0),
                    "discovery": stats.get("meeting", 0),
                }
            except Exception as e:
                logger.error(f"Supabase stats failed: {e}")
        return {
            "researched": 0,
            "sequenced": 0,
            "engaged": 0,
            "discovery": 0,
        }
