"""
OVPN filename search/filter (mirrors frontend src/utils/ovpnFiles.js token rules).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

_US_STATE_SLUGS = [
    "ALABAMA",
    "ALASKA",
    "ARIZONA",
    "ARKANSAS",
    "CALIFORNIA",
    "COLORADO",
    "CONNECTICUT",
    "DELAWARE",
    "DISTRICT_OF_COLUMBIA",
    "FLORIDA",
    "GEORGIA",
    "HAWAII",
    "IDAHO",
    "ILLINOIS",
    "INDIANA",
    "IOWA",
    "KANSAS",
    "KENTUCKY",
    "LOUISIANA",
    "MAINE",
    "MARYLAND",
    "MASSACHUSETTS",
    "MICHIGAN",
    "MINNESOTA",
    "MISSISSIPPI",
    "MISSOURI",
    "MONTANA",
    "NEBRASKA",
    "NEVADA",
    "NEW_HAMPSHIRE",
    "NEW_JERSEY",
    "NEW_MEXICO",
    "NEW_YORK",
    "NORTH_CAROLINA",
    "NORTH_DAKOTA",
    "OHIO",
    "OKLAHOMA",
    "OREGON",
    "PENNSYLVANIA",
    "RHODE_ISLAND",
    "SOUTH_CAROLINA",
    "SOUTH_DAKOTA",
    "TENNESSEE",
    "TEXAS",
    "UTAH",
    "VERMONT",
    "VIRGINIA",
    "WASHINGTON",
    "WEST_VIRGINIA",
    "WISCONSIN",
    "WYOMING",
]

US_STATE_SLUGS_SORTED = sorted(_US_STATE_SLUGS, key=len, reverse=True)

_PROTON_NAME_RE = re.compile(
    r"^(\d+)-([a-z]{2})-([a-z0-9_-]+)\.protonvpn\.(tcp|udp)\.ovpn$",
    re.IGNORECASE,
)

# Optional: de_city.ovpn, fr-paris.ovpn (two-letter ISO at start, then - or _).
_ISO2_PREFIX_RE = re.compile(r"^([a-z]{2})[-_].", re.IGNORECASE)

# Country_City… or Country_Region_City… basenames (strip .ovpn) → ISO 3166-1 alpha-2.
# Longest slug first at runtime via _COUNTRY_SLUG_PREFIXES_SORTED.
_COUNTRY_SLUG_TO_ISO2: Dict[str, str] = {
    "United_Arab_Emirates": "AE",
    "United_Kingdom": "GB",
    "Czech_Republic": "CZ",
    "South_Korea": "KR",
    "New_Zealand": "NZ",
    "South_Africa": "ZA",
    "Sri_Lanka": "LK",
    "Saudi_Arabia": "SA",
    "Puerto_Rico": "PR",
    "Philippines": "PH",
    "Netherlands": "NL",
    "Switzerland": "CH",
    "Australia": "AU",
    "Argentina": "AR",
    "Austria": "AT",
    "Belgium": "BE",
    "Bolivia": "BO",
    "Brazil": "BR",
    "Bulgaria": "BG",
    "Canada": "CA",
    "Chile": "CL",
    "Colombia": "CO",
    "Cyprus": "CY",
    "Denmark": "DK",
    "Ecuador": "EC",
    "France": "FR",
    "Georgia": "GE",
    "Germany": "DE",
    "Greece": "GR",
    "Hong_Kong": "HK",
    "Hungary": "HU",
    "Ireland": "IE",
    "Italy": "IT",
    "Japan": "JP",
    "Malaysia": "MY",
    "Mexico": "MX",
    "Morocco": "MA",
    "Nigeria": "NG",
    "Norway": "NO",
    "Peru": "PE",
    "Poland": "PL",
    "Portugal": "PT",
    "Romania": "RO",
    "Singapore": "SG",
    "Spain": "ES",
    "Sweden": "SE",
    "Taiwan": "TW",
    "Thailand": "TH",
    "Turkey": "TR",
    "Ukraine": "UA",
    "Algeria": "DZ",
}

_COUNTRY_SLUG_PREFIXES_SORTED: List[str] = sorted(
    _COUNTRY_SLUG_TO_ISO2.keys(),
    key=len,
    reverse=True,
)

# Display labels for Configuration / API; unknown codes fall back to the code itself.
ISO2_DISPLAY_LABELS: Dict[str, str] = {
    "US": "United States",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "NL": "Netherlands",
    "CH": "Switzerland",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "ES": "Spain",
    "IT": "Italy",
    "PL": "Poland",
    "AT": "Austria",
    "BE": "Belgium",
    "IE": "Ireland",
    "PT": "Portugal",
    "CZ": "Czechia",
    "RO": "Romania",
    "CA": "Canada",
    "AU": "Australia",
    "JP": "Japan",
    "SG": "Singapore",
    "IN": "India",
    "BR": "Brazil",
    "MX": "Mexico",
    "AR": "Argentina",
    "AE": "United Arab Emirates",
    "KR": "South Korea",
    "NZ": "New Zealand",
    "ZA": "South Africa",
    "LK": "Sri Lanka",
    "SA": "Saudi Arabia",
    "PR": "Puerto Rico",
    "PH": "Philippines",
    "HK": "Hong Kong",
    "TW": "Taiwan",
    "TR": "Turkey",
    "UA": "Ukraine",
    "GE": "Georgia",
    "MY": "Malaysia",
    "CL": "Chile",
    "CO": "Colombia",
    "CY": "Cyprus",
    "EC": "Ecuador",
    "GR": "Greece",
    "HU": "Hungary",
    "MA": "Morocco",
    "NG": "Nigeria",
    "PE": "Peru",
    "BO": "Bolivia",
    "BG": "Bulgaria",
    "DZ": "Algeria",
}


def _humanize_slug(slug: str) -> str:
    return slug.replace("_", " ").title()


def format_ovpn_display_label(filename: str) -> str:
    if not filename:
        return ""
    s = re.sub(r"\.protonvpn\.(tcp|udp)\.ovpn$", "", filename, flags=re.IGNORECASE)
    s = re.sub(r"\.ovpn$", "", s, flags=re.IGNORECASE)
    return s


def parse_proton_ovpn_meta(filename: str) -> Optional[Dict[str, Any]]:
    m = _PROTON_NAME_RE.match(filename)
    if not m:
        return None
    region = m.group(3).replace("_", " ").upper()
    return {
        "id": m.group(1),
        "country": m.group(2).upper(),
        "region": region,
        "proto": m.group(4).lower(),
    }


def parse_united_states_ovpn_meta(filename: str) -> Optional[Dict[str, Any]]:
    if not filename.startswith("United_States_") or not filename.endswith(".ovpn"):
        return None
    rest = filename[len("United_States_") : -len(".ovpn")]
    for state_slug in US_STATE_SLUGS_SORTED:
        prefix = state_slug + "_"
        # Filenames use Title_Case (e.g. California_Los_Angeles), not ALL_CAPS slugs.
        if rest.upper().startswith(prefix):
            city_slug = rest[len(prefix) :]
            if not city_slug:
                return None
            return {
                "state": _humanize_slug(state_slug),
                "city": _humanize_slug(city_slug),
                "stateSlug": state_slug,
                "citySlug": city_slug,
            }
    return None


def _infer_country_from_place_slug_basename(base: str) -> Optional[str]:
    """Match Country_City… basenames like Germany_Frankfurt or United_Kingdom_England_London."""
    for slug in _COUNTRY_SLUG_PREFIXES_SORTED:
        if base == slug or base.startswith(slug + "_"):
            return _COUNTRY_SLUG_TO_ISO2[slug]
    return None


def country_code_display_label(code: str) -> str:
    c = (code or "").strip().upper()
    if len(c) != 2:
        return c or ""
    return ISO2_DISPLAY_LABELS.get(c, c)


def infer_ovpn_country_code(filename: str) -> Optional[str]:
    """Best-effort ISO 3166-1 alpha-2 from filename, or None if unknown."""
    if not filename or not filename.endswith(".ovpn"):
        return None
    proton = parse_proton_ovpn_meta(filename)
    if proton:
        return proton["country"]
    base = filename[: -len(".ovpn")]
    if base.startswith("United_States_") or base == "United_States":
        return "US"
    slug_iso = _infer_country_from_place_slug_basename(base)
    if slug_iso:
        return slug_iso
    m = _ISO2_PREFIX_RE.match(filename)
    if m:
        return m.group(1).upper()
    return None


def filter_ovpn_files_by_country(files: List[str], country: str) -> List[str]:
    """If country is empty or 'random', return files unchanged; else keep matching ISO2."""
    raw = (country or "").strip().lower()
    if not raw or raw == "random":
        return list(files)
    want = raw.upper()
    out: List[str] = []
    for f in files:
        code = infer_ovpn_country_code(f)
        if code == want:
            out.append(f)
    return out


def build_ovpn_country_options(files: List[str]) -> List[Dict[str, Any]]:
    """
    {code, label, count} for the Random-pool country picker.

    Always includes every entry in ISO2_DISPLAY_LABELS (count may be 0) so the UI lists
    United States and other common countries even when the scan is empty or filenames
    are not yet classified. Also appends any inferred country code from files that is
    not in that map (e.g. a Proton profile for a country we did not label).
    """
    counts: Dict[str, int] = {}
    for f in files:
        code = infer_ovpn_country_code(f)
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1

    all_codes: Set[str] = set(ISO2_DISPLAY_LABELS.keys()) | set(counts.keys())
    rows: List[Dict[str, Any]] = [
        {
            "code": code,
            "label": country_code_display_label(code),
            "count": counts.get(code, 0),
        }
        for code in sorted(
            all_codes,
            key=lambda c: (0 if c == "US" else 1, country_code_display_label(c).lower()),
        )
    ]
    return rows


def normalize_randomize_country(value: Any) -> str:
    """Config value -> 'random' or uppercase ISO2."""
    s = str(value or "").strip().lower()
    if not s or s == "random":
        return "random"
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return "random"


def randomize_country_status_label(value: Any) -> str:
    """Human-readable line for /api/status."""
    n = normalize_randomize_country(value)
    if n == "random":
        return "any country"
    return country_code_display_label(n)


def ovpn_file_search_haystack(filename: str) -> str:
    if not filename:
        return ""
    parts: List[str] = [filename, format_ovpn_display_label(filename)]
    meta = parse_proton_ovpn_meta(filename)
    if meta:
        parts.extend(
            [
                meta["id"],
                meta["country"],
                meta["region"],
                meta["proto"],
                f"{meta['country']} {meta['region']}",
                f"{meta['id']} {meta['country']}",
                f"{meta['id']}-{meta['country']}".lower(),
            ]
        )
    us = parse_united_states_ovpn_meta(filename)
    if us:
        st = us["state"]
        ct = us["city"]
        parts.extend(
            [
                st.lower(),
                ct.lower(),
                f"{st} {ct}".lower(),
                us["stateSlug"].lower().replace("_", " "),
                us["citySlug"].lower().replace("_", " "),
                us["stateSlug"].lower(),
                us["citySlug"].lower(),
            ]
        )
    ic = infer_ovpn_country_code(filename)
    if ic:
        parts.append(ic.lower())
        parts.append(country_code_display_label(ic).lower())
    return " ".join(parts).lower()


def filter_ovpn_files_by_query(files: List[str], query: str) -> List[str]:
    if not isinstance(files, list):
        return []
    raw = str(query).strip().lower()
    if not raw:
        return list(files)
    tokens = [t for t in raw.split() if t]
    if not tokens:
        return list(files)
    out: List[str] = []
    for f in files:
        hay = ovpn_file_search_haystack(f)
        if all(t in hay for t in tokens):
            out.append(f)
    return out
