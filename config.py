"""
ICP configurations and global settings.
All secrets come from environment variables (set in Railway).
"""
import os

# API Keys (set these in Railway, not here)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
# HUBSPOT_ACCESS_TOKEN matches the README; HUBSPOT_API_KEY kept as a fallback for existing deployments
HUBSPOT_API_KEY = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Settings
RUN_MODE = os.getenv("RUN_MODE", "production")
MAX_PROSPECTS_PER_ICP = int(os.getenv("MAX_PROSPECTS_PER_ICP", "30"))
ENRICHMENT_DELAY = float(os.getenv("ENRICHMENT_DELAY", "1.0"))

# Your company identity (used in Claude enrichment prompts)
COMPANY_NAME = os.getenv("COMPANY_NAME", "Your Company")
COMPANY_DESCRIPTION_SHORT = os.getenv(
    "COMPANY_DESCRIPTION_SHORT",
    "Describe your company and what you sell in one sentence."
)

# ICP Definitions
# Define one or more ICPs below. Each ICP needs:
#   - name: Human-readable label
#   - emoji: For Slack digest headers
#   - product_focus: What you're selling to this ICP
#   - pain_points: Problems they have that you solve
#   - value_props: Why your solution is better
#   - apollo_filters: Title, seniority, and company size filters for Apollo search
#   - target_companies: Named companies to prioritize (fuzzy-matched)
#
# See README.md for a full walkthrough of adding your own ICP.

ICP_CONFIGS = {
    "mobile_game_studios": {
        "name": "Mobile Game Studio Leaders",
        "emoji": "🎮",
        "product_focus": (
            "Your product focus here, e.g. "
            "UA analytics and creative optimization for mobile games"
        ),
        "pain_points": [
            "Rising CPIs making user acquisition unsustainable",
            "Creative fatigue across ad networks",
            "Difficulty attributing ROAS post-ATT/SKAN",
            "Scaling beyond core geo markets",
        ],
        "value_props": [
            "Your value prop 1",
            "Your value prop 2",
            "Your value prop 3",
        ],
        "apollo_filters": {
            "person_titles": [
                "VP Marketing",
                "Head of Marketing",
                "Director Marketing",
                "VP User Acquisition",
                "Head of User Acquisition",
                "Director User Acquisition",
                "VP Growth",
                "Head of Growth",
                "Director Growth",
                "VP Creative",
                "Head of Creative",
                "Director Creative",
                "VP Ad Monetization",
                "Head of Ad Monetization",
                "Director Ad Monetization",
                "Chief Marketing Officer",
                "Chief Revenue Officer",
                "VP Performance Marketing",
                "Head of Performance Marketing",
            ],
            "person_seniorities": ["vp", "director", "c_suite"],
            "organization_num_employees_ranges": ["20,5000"],
        },
        "target_companies": [
            # Tier 1: Major mobile game publishers
            "King",
            "Supercell",
            "MiHoYo",
            "HoYoverse",
            "Niantic",
            "Zynga",
            "Take-Two Interactive",
            "Kabam",
            "Jam City",
            "Scopely",
            "Playtika",
            "Moon Active",
            "Dream Games",
            "Peak Games",
            "Rovio",
            "Netmarble",
            "Nexon",
            "NCSOFT",
            "Com2uS",
            "Garena",
            # Tier 2: Mid-market studios
            "Voodoo",
            "AppLovin",
            "ironSource",
            "Playrix",
            "Socialpoint",
            "Product Madness",
            "Plarium",
            "Tilting Point",
            "Kwalee",
            "Tripledot Studios",
            "CrazyLabs",
            "Homa",
            "Lion Studios",
            "Miniclip",
            "Outfit7",
            "Wildlife Studios",
            "Pixonic",
            "MY.GAMES",
            "Huuuge Games",
            "SciPlay",
            # Tier 3: Emerging and indie studios with scale
            "Habby",
            "Archero",
            "Lightfox Games",
            "Century Games",
            "FunPlus",
            "IGG",
            "Lilith Games",
            "37Games",
            "Yotta Games",
            "Top Games",
        ],
    },
}
