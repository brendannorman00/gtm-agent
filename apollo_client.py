"""
Apollo API client. Handles prospect search, enrichment, and contact management.

Uses the new mixed_people/api_search endpoint (the old mixed_people/search
is deprecated for API callers). This endpoint takes query parameters,
not a JSON body. It does NOT return emails/phones - use the enrichment
endpoint separately for that.
"""
import requests
import logging
from urllib.parse import urlencode
from config import APOLLO_API_KEY

logger = logging.getLogger(__name__)

APOLLO_BASE = "https://api.apollo.io/api/v1"
HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-cache",
    "X-Api-Key": APOLLO_API_KEY,
}

# Industries that indicate a prospect is outside our ICP.
# Applied as a post-search filter before enrichment or Supabase writes.
EXCLUDED_INDUSTRIES = {
    "consumer goods",
    "retail",
    "sports",
    "financial services",
    "insurance",
    "real estate",
    "hospital & health care",
    "nonprofit organization management",
    "government administration",
    "higher education",
    "defense & space",
    "automotive",
    "food & beverages",
    "apparel & fashion",
    "pharmaceuticals",
    "medical devices",
    "law practice",
    "accounting",
    "construction",
    "oil & energy",
    "utilities",
    "transportation/trucking/railroad",
    "logistics and supply chain",
    "religious institutions",
    "individual & family services",
    "civic & social organization",
}

# Industries that strongly indicate adtech/media/publishing relevance.
# Prospects at these companies pass through even if not on the target list.
ADTECH_MEDIA_INDUSTRIES = {
    "online media",
    "media production",
    "broadcast media",
    "newspapers",
    "publishing",
    "entertainment",
    "internet",
    "marketing and advertising",
    "information technology and services",
    "computer software",
}


def _build_query_string(filters, page=1, per_page=50):
    """
    Build URL query string with bracket notation for arrays.
    Apollo expects: person_titles[]=VP+Product&person_titles[]=CTO
    """
    params = []

    # Array parameters (use bracket notation)
    array_fields = [
        "person_titles",
        "person_seniorities",
        "organization_num_employees_ranges",
        "person_locations",
    ]
    for field in array_fields:
        if field in filters:
            for value in filters[field]:
                params.append((f"{field}[]", value))

    # Pagination
    params.append(("page", str(page)))
    params.append(("per_page", str(per_page)))

    return urlencode(params)


def _normalize(name):
    """Lowercase and strip common suffixes for fuzzy company matching."""
    name = (name or "").lower().strip()
    for suffix in [", inc.", ", inc", " inc.", " inc", ", llc", " llc",
                   " corp.", " corp", " ltd.", " ltd", " plc", " group",
                   " media", " digital"]:
        name = name.removesuffix(suffix)
    return name.strip()


def _matches_target_company(prospect_company, target_companies):
    """
    Check if prospect's company fuzzy-matches any target company.
    Uses normalized substring matching in both directions.
    """
    norm_prospect = _normalize(prospect_company)
    if not norm_prospect:
        return False

    for target in target_companies:
        norm_target = _normalize(target)
        # Exact match after normalization
        if norm_prospect == norm_target:
            return True
        # Substring match (either direction for short names)
        if len(norm_target) >= 4 and norm_target in norm_prospect:
            return True
        if len(norm_prospect) >= 4 and norm_prospect in norm_target:
            return True

    return False


def _is_valid_prospect(prospect):
    """
    Post-search ICP validation. Returns False if the prospect's industry
    is in the excluded list, logging the reason for visibility.
    """
    industry = (prospect.get("industry") or "").lower().strip()
    if industry in EXCLUDED_INDUSTRIES:
        logger.debug(
            f"Filtered out {prospect.get('name')} at {prospect.get('company')} "
            f"— industry '{industry}' is excluded"
        )
        return False
    return True


def search_prospects(icp_key, icp_config, page=1, per_page=50):
    """
    Search Apollo for prospects matching ICP filters.
    Uses the new mixed_people/api_search endpoint.
    Returns list of prospect dicts, post-filtered by industry and
    target company match.
    """
    filters = icp_config["apollo_filters"]
    target_companies = icp_config.get("target_companies", [])
    query_string = _build_query_string(filters, page, per_page)
    url = f"{APOLLO_BASE}/mixed_people/api_search?{query_string}"

    logger.info(f"Apollo search URL: {url[:200]}...")

    try:
        resp = requests.post(
            url,
            headers=HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(
                f"Apollo search failed for {icp_key}: "
                f"status={resp.status_code} body={resp.text[:500]}"
            )
            return []
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Apollo search failed for {icp_key}: {e}")
        return []

    total = data.get("total_entries", 0)
    logger.info(f"Apollo found {total} total matches for {icp_key}")

    prospects = []
    for person in data.get("people", []):
        org = person.get("organization", {})
        prospects.append({
            "apollo_id": person.get("id"),
            "name": person.get("name") or f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
            "first_name": person.get("first_name", ""),
            "last_name": person.get("last_name", ""),
            "title": person.get("title", "Unknown"),
            "email": person.get("email"),  # May be None from this endpoint
            "linkedin_url": person.get("linkedin_url"),
            "headline": person.get("headline", ""),
            "company": org.get("name", "Unknown") if isinstance(org, dict) else "Unknown",
            "company_domain": org.get("primary_domain") if isinstance(org, dict) else None,
            "company_description": org.get("short_description", "") if isinstance(org, dict) else "",
            "industry": org.get("industry", "") if isinstance(org, dict) else "",
            "employee_count": org.get("estimated_num_employees") if isinstance(org, dict) else None,
            "technologies": [],  # Not returned by api_search
            "funding_info": "",  # Not returned by api_search
            "city": person.get("city", ""),
            "state": person.get("state", ""),
            "country": person.get("country", ""),
        })

    # Step 1: Filter out excluded industries
    before_industry = len(prospects)
    prospects = [p for p in prospects if _is_valid_prospect(p)]
    after_industry = len(prospects)

    if before_industry != after_industry:
        logger.info(
            f"Industry filter removed {before_industry - after_industry} prospects "
            f"for {icp_key} ({after_industry} remaining)"
        )

    # Step 2: Filter to target companies + adtech/media industry prospects
    if target_companies:
        target_matched = []
        industry_matched = []
        filtered_out = []

        for p in prospects:
            if _matches_target_company(p["company"], target_companies):
                target_matched.append(p)
            elif (p.get("industry") or "").lower().strip() in ADTECH_MEDIA_INDUSTRIES:
                industry_matched.append(p)
            else:
                filtered_out.append(p)

        # Prioritize target company matches, then industry matches
        prospects = target_matched + industry_matched

        if filtered_out:
            filtered_names = [f"{p['name']}@{p['company']}" for p in filtered_out[:5]]
            logger.info(
                f"Company filter removed {len(filtered_out)} non-target prospects "
                f"for {icp_key} (kept {len(target_matched)} target + "
                f"{len(industry_matched)} industry match). "
                f"Filtered examples: {', '.join(filtered_names)}"
            )
        if target_matched:
            matched_names = [f"{p['name']}@{p['company']}" for p in target_matched[:5]]
            logger.info(
                f"Target company matches for {icp_key}: {', '.join(matched_names)}"
            )

    logger.info(f"Apollo returned {len(prospects)} qualified prospects for {icp_key}")
    return prospects


def enrich_person(person_id):
    """
    Enrich a person by Apollo ID to get email, phone, and full profile.
    This consumes Apollo credits.
    Returns enriched person dict or None.
    """
    try:
        resp = requests.post(
            f"{APOLLO_BASE}/people/match",
            headers=HEADERS,
            json={"id": person_id, "reveal_personal_emails": False},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(
                f"Apollo enrichment failed: "
                f"status={resp.status_code} body={resp.text[:300]}"
            )
            return None

        data = resp.json()
        person = data.get("person", {})

        if not person:
            return None

        org = person.get("organization", {}) or {}

        return {
            "email": person.get("email"),
            "phone": (
                person.get("phone_numbers", [{}])[0].get("sanitized_number")
                if person.get("phone_numbers")
                else None
            ),
            "linkedin_url": person.get("linkedin_url"),
            "company_description": org.get("short_description", ""),
            "industry": org.get("industry", ""),
            "employee_count": org.get("estimated_num_employees"),
            "technologies": [
                t.get("name")
                for t in org.get("current_technologies", [])
                if t.get("name")
            ],
            "funding_info": org.get("total_funding_printed", ""),
            "company_revenue": org.get("annual_revenue_printed", ""),
        }
    except requests.RequestException as e:
        logger.error(f"Apollo enrichment failed: {e}")
        return None


def create_contact(prospect_data):
    """Create a contact in Apollo."""
    try:
        resp = requests.post(
            f"{APOLLO_BASE}/contacts",
            headers=HEADERS,
            json={
                "first_name": prospect_data.get("first_name"),
                "last_name": prospect_data.get("last_name"),
                "title": prospect_data.get("title"),
                "email": prospect_data.get("email"),
                "organization_name": prospect_data.get("company"),
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("contact", {}).get("id")
    except requests.RequestException as e:
        logger.error(f"Failed to create Apollo contact: {e}")
        return None


def add_to_sequence(contact_id, sequence_id):
    """Add a contact to an Apollo email sequence."""
    try:
        resp = requests.post(
            f"{APOLLO_BASE}/emailer_campaigns/{sequence_id}/add_contact_ids",
            headers=HEADERS,
            json={
                "contact_ids": [contact_id],
                "sequence_active_in_other_campaigns": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"Added contact {contact_id} to sequence {sequence_id}")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to add to sequence: {e}")
        return False
