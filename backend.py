"""OPENAI_API_KEY
FontScan – Enhanced Backend
Run: python backend.py  →  http://localhost:8000

New in this version:
  • /api/estimate-traffic  — AI-powered monthly visit estimation (GPT-4o)
  • Font Type tagging       — "Embedded Font" vs "CSS Font" per entry
  • Restricted fonts        — passed through with is_restricted=True flag
                              (suppressed from "All Fonts"; shown in "Restricted" section)
  • Font Family field       — raw family name emitted alongside display name
  • Modular helpers         — classify_font_type(), format_traffic(), estimate_monthly_traffic()
"""

import re, os, json, datetime, asyncio, concurrent.futures
import requests as req_lib
from io import BytesIO
from urllib.parse import urlparse, parse_qs
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv

import undetected_chromedriver as uc
from selenium_stealth import stealth
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException, TimeoutException, NoSuchWindowException
from fontTools.ttLib import TTFont

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = "gpt-4o"
AI_CACHE_FILE  = "font_ai_cache.json"
TRAFFIC_CACHE_FILE = "traffic_cache.json"

app = FastAPI(title="FontScan API")

# ─────────────────────────────────────────────────────────────
#  UNKNOWN VALUE HELPERS
# ─────────────────────────────────────────────────────────────
_UNKNOWN_VALUES = {
    "", "unknown", "check manually", "—", "-", "n/a",
    "not found", "unresolved", "none", "null",
}

def _is_unknown(val: str) -> bool:
    return (val or "").strip().lower() in _UNKNOWN_VALUES


# ─────────────────────────────────────────────────────────────
#  FONT TYPE CLASSIFICATION
# ─────────────────────────────────────────────────────────────
def classify_font_type(kind: str) -> str:
    """
    Returns 'Embedded Font' or 'CSS Font' based on discovery method.

    Embedded Font  →  actual binary font file loaded by the page,
                      or a self-hosted @font-face block.
    CSS Font       →  font referenced only inside an external stylesheet
                      (Google Fonts, CDN import, plain CSS font-family decl).
    """
    css_kinds = {"google-fonts", "css-decl"}
    return "CSS Font" if kind in css_kinds else "Embedded Font"


# ─────────────────────────────────────────────────────────────
#  TRAFFIC ESTIMATION
# ─────────────────────────────────────────────────────────────
_traffic_cache: dict = {}

def _load_traffic_cache():
    global _traffic_cache
    if os.path.exists(TRAFFIC_CACHE_FILE):
        try:
            with open(TRAFFIC_CACHE_FILE) as f:
                _traffic_cache = json.load(f)
            print(f"✓ Loaded {len(_traffic_cache)} cached traffic estimates")
        except Exception:
            _traffic_cache = {}

def _save_traffic_cache():
    try:
        with open(TRAFFIC_CACHE_FILE, "w") as f:
            json.dump(_traffic_cache, f, indent=2)
    except Exception:
        pass

_load_traffic_cache()


def format_traffic(n: int) -> str:
    """Format a raw integer into human-readable shorthand: 1200000 → '1.2M'."""
    if n >= 1_000_000_000:
        v = round(n / 1_000_000_000, 1)
        return f"{v}B"
    if n >= 1_000_000:
        v = round(n / 1_000_000, 1)
        return f"{v}M"
    if n >= 1_000:
        v = round(n / 1_000, 1)
        return f"{v}K"
    return str(n)


_TRAFFIC_PROMPT = """\
You are a professional web traffic analyst with expertise equivalent to SimilarWeb, Ahrefs, and Semrush.

Estimate the MONTHLY WEBSITE VISITS for the following URL:

URL     : {url}
Domain  : {domain}
TLD     : {tld}

Use these calibration benchmarks:
- google.com / youtube.com  : 30B–90B / month
- amazon.com / wikipedia.org: 2B–5B  / month
- Major news sites (bbc.com, nytimes.com): 200M–500M / month
- Popular SaaS / developer tools: 5M–50M / month
- Regional brands / mid-size e-commerce: 500K–5M / month
- Small business sites / niche blogs: 50K–500K / month
- Local or new sites: 5K–50K / month
- Very niche / micro sites: under 5K / month

Instructions:
1. If the domain belongs to a globally or regionally recognized brand, use your knowledge to give a precise estimate.
2. For unknown domains, infer from: TLD, keywords in the domain name, typical niche scale.
3. Return ONLY a single plain integer — no commas, no text, no symbols.

Examples of valid responses:  45000000   1250000   87000   12000
"""


def estimate_monthly_traffic(url: str) -> str:
    """
    Synchronous call to OpenAI to estimate monthly visits for a URL.
    Returns formatted string ('120K', '2.5M') or 'N/A' on failure.
    Caches results by domain to avoid duplicate API calls.
    """
    api_key = OPENAI_API_KEY
    if not api_key or api_key.strip() in ("", "sk-YOUR-KEY-HERE"):
        return "N/A"

    try:
        parsed = urlparse(url if url.startswith("http") else "https://" + url)
        domain = parsed.netloc.replace("www.", "")
        tld    = domain.split(".")[-1] if "." in domain else "com"
    except Exception:
        domain, tld = url, "com"

    # Return cached result if available
    cache_key = domain.lower()
    if cache_key in _traffic_cache:
        cached_raw = _traffic_cache[cache_key].get("raw")
        if cached_raw:
            return format_traffic(cached_raw)

    prompt = _TRAFFIC_PROMPT.format(url=url, domain=domain, tld=tld)

    try:
        r = req_lib.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       OPENAI_MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens":  20,
            },
            timeout=20,
        )

        if r.status_code != 200:
            print(f"⚠ Traffic estimate API error {r.status_code}: {r.text[:200]}")
            return "N/A"

        raw_text = r.json()["choices"][0]["message"]["content"].strip()
        # Extract first integer-like sequence
        match = re.search(r"\d[\d,]*", raw_text)
        if not match:
            return "N/A"

        raw_n = int(match.group().replace(",", ""))
        formatted = format_traffic(raw_n)

        # Cache
        _traffic_cache[cache_key] = {
            "raw":       raw_n,
            "formatted": formatted,
            "url":       url,
            "ts":        datetime.datetime.now().isoformat(),
        }
        _save_traffic_cache()

        print(f"📊 Traffic estimate → {domain}: {formatted} ({raw_n:,}/mo)")
        return formatted

    except Exception as e:
        print(f"✗ Traffic estimate failed for {url}: {e}")
        return "N/A"


# ─────────────────────────────────────────────────────────────
#  CDN PATTERNS & FONT DATABASE  (unchanged from original)
# ─────────────────────────────────────────────────────────────
CDN_PATTERNS = [
    ("fonts.gstatic.com",    "Google",            "Free (OFL)"),
    ("fonts.googleapis.com", "Google",            "Free (OFL)"),
    ("fonts.bunny.net",      "Bunny Fonts (CDN)", "Free (OFL)"),
    ("use.typekit.net",      "Adobe (Typekit)",   "Paid"),
    ("p.typekit.net",        "Adobe (Typekit)",   "Paid"),
    ("fast.fonts.net",       "Monotype",          "Paid"),
    ("fonts.monotype.com",   "Monotype",          "Paid"),
    ("cloud.typography.com", "Hoefler & Co.",     "Paid"),
    ("typography.com",       "Hoefler & Co.",     "Paid"),
    ("fonts.adobe.com",      "Adobe",             "Paid"),
    ("use.fontawesome.com",  "Font Awesome",      "Free/Paid"),
    ("kit.fontawesome.com",  "Font Awesome",      "Free/Paid"),
    ("cdn.jsdelivr.net",     "jsDelivr CDN",      "Free (OFL)"),
    ("cdnfonts.com",         "CDN Fonts",         "Free"),
]

FONT_DB = {
    "segoe ui":("Microsoft","Paid"),
    "segoe ui variable":("Microsoft","Paid"),
    "segoe print":("Microsoft","Paid"),
    "segoe script":("Microsoft","Paid"),
    "calibri":("Microsoft","Paid"),
    "cambria":("Microsoft","Paid"),
    "cambria math":("Microsoft","Paid"),
    "candara":("Microsoft","Paid"),
    "consolas":("Microsoft","Paid"),
    "constantia":("Microsoft","Paid"),
    "corbel":("Microsoft","Paid"),
    "georgia":("Microsoft","Paid"),
    "verdana":("Microsoft","Paid"),
    "trebuchet ms":("Microsoft","Paid"),
    "tahoma":("Microsoft","Paid"),
    "comic sans ms":("Microsoft","Paid"),
    "impact":("Monotype / Microsoft","Paid"),
    "franklin gothic medium":("ITC / Microsoft","Paid"),
    "courier new":("Monotype / Microsoft","Restricted"),
    "lucida console":("Bigelow & Holmes","Paid"),
    "-apple-system":("Apple","OS Default"),
    "blinkmacsystemfont":("Apple","OS Default"),
    "sf pro":("Apple","Restricted"),
    "sf pro text":("Apple","Restricted"),
    "sf pro display":("Apple","Restricted"),
    "sf mono":("Apple","Restricted"),
    "sf compact":("Apple","Restricted"),
    "new york":("Apple","Restricted"),
    "helvetica":("Monotype / Linotype","Paid"),
    "helvetica neue":("Linotype","Paid"),
    "lucida grande":("Bigelow & Holmes","Paid"),
    "monaco":("Apple","Restricted"),
    "menlo":("Apple","Restricted"),
    "arial":("Monotype","Paid"),
    "arial unicode ms":("Monotype","Paid"),
    "arial narrow":("Monotype","Paid"),
    "arial black":("Monotype","Paid"),
    "times new roman":("Monotype","Paid"),
    "times":("Linotype","Paid"),
    "gill sans":("Monotype","Paid"),
    "gill sans nova":("Monotype","Paid"),
    "frutiger":("Linotype","Paid"),
    "neue haas grotesk":("Linotype","Paid"),
    "palatino":("Linotype","Paid"),
    "palatino linotype":("Linotype","Paid"),
    "optima":("Linotype","Paid"),
    "univers":("Linotype","Paid"),
    "avenir":("Linotype","Paid"),
    "avenir next":("Linotype","Paid"),
    "bodoni":("Berthold / Linotype","Paid"),
    "akzidenz grotesk":("Berthold","Paid"),
    "myriad pro":("Adobe","Paid"),
    "minion pro":("Adobe","Paid"),
    "garamond":("Linotype / Adobe","Paid"),
    "adobe garamond":("Adobe","Paid"),
    "futura":("Bauer / Neufville Digital","Paid"),
    "baskerville":("Bitstream / Linotype","Paid"),
    "perpetua":("Monotype","Paid"),
    "rockwell":("Monotype","Paid"),
    "century gothic":("Monotype","Paid"),
    "copperplate gothic":("Monotype","Paid"),
    "gotham":("Hoefler & Co.","Paid"),
    "gotham rounded":("Hoefler & Co.","Paid"),
    "gotham narrow":("Hoefler & Co.","Paid"),
    "mercury":("Hoefler & Co.","Paid"),
    "sentinel":("Hoefler & Co.","Paid"),
    "whitney":("Hoefler & Co.","Paid"),
    "ideal sans":("Hoefler & Co.","Paid"),
    "chronicle":("Hoefler & Co.","Paid"),
    "surveyor":("Hoefler & Co.","Paid"),
    "graphik":("Commercial Type","Paid"),
    "druk":("Commercial Type","Paid"),
    "lyon":("Commercial Type","Paid"),
    "atlas grotesk":("Commercial Type","Paid"),
    "canela":("Commercial Type","Paid"),
    "caponi":("Commercial Type","Paid"),
    "proxima nova":("Mark Simonson Studio","Paid"),
    "proxima soft":("Mark Simonson Studio","Paid"),
    "brandon grotesque":("HVD Fonts","Paid"),
    "sofia pro":("Mostardesign","Paid"),
    "circular":("Lineto","Paid"),
    "brown":("Lineto","Paid"),
    "aktiv grotesk":("Dalton Maag","Paid"),
    "effra":("Dalton Maag","Paid"),
    "ivar":("Dalton Maag","Paid"),
    "apercu":("Colophon Foundry","Paid"),
    "roobert":("Displaay Type Foundry","Paid"),
    "whyte":("Dinamo","Paid"),
    "helvetica now":("Monotype","Paid"),
    "neue haas unica":("Monotype","Paid"),
    "tt commons":("TypeType","Paid"),
    "tt norms":("TypeType","Paid"),
    "tt hoves":("TypeType","Paid"),
    "gt america":("Grilli Type","Paid"),
    "gt walsheim":("Grilli Type","Paid"),
    "gt super":("Grilli Type","Paid"),
    "gt pressura":("Grilli Type","Paid"),
    "tobias":("Grilli Type","Paid"),
    "founders grotesk":("Klim Type Foundry","Paid"),
    "tiempos":("Klim Type Foundry","Paid"),
    "tiempos text":("Klim Type Foundry","Paid"),
    "national 2":("Klim Type Foundry","Paid"),
    "domaine":("Klim Type Foundry","Paid"),
    "calibre":("Klim Type Foundry","Paid"),
    "poppins":("Indian Type Foundry","Free (OFL)"),
    "clash display":("Indian Type Foundry","Free (OFL)"),
    "general sans":("Indian Type Foundry","Free (OFL)"),
    "satoshi":("Indian Type Foundry","Free (OFL)"),
    "plus jakarta sans":("Gumpita Rahayu / ITF","Free (OFL)"),
    "roboto":("Christian Robertson / Google","Free (OFL)"),
    "roboto mono":("Christian Robertson / Google","Free (OFL)"),
    "roboto slab":("Christian Robertson / Google","Free (OFL)"),
    "open sans":("Steve Matteson / Google","Free (OFL)"),
    "lato":("Łukasz Dziedzic","Free (OFL)"),
    "montserrat":("Julieta Ulanovsky","Free (OFL)"),
    "noto sans":("Google","Free (OFL)"),
    "noto serif":("Google","Free (OFL)"),
    "noto color emoji":("Google","Free (OFL)"),
    "noto sans mono":("Google","Free (OFL)"),
    "oswald":("Vernon Adams / Google","Free (OFL)"),
    "raleway":("The League of Moveable Type","Free (OFL)"),
    "pt sans":("ParaType","Free (OFL)"),
    "pt serif":("ParaType","Free (OFL)"),
    "source sans pro":("Adobe","Free (OFL)"),
    "source sans 3":("Adobe","Free (OFL)"),
    "source serif pro":("Adobe","Free (OFL)"),
    "source serif 4":("Adobe","Free (OFL)"),
    "source code pro":("Adobe","Free (OFL)"),
    "merriweather":("Sorkin Type","Free (OFL)"),
    "merriweather sans":("Sorkin Type","Free (OFL)"),
    "playfair display":("Claus Eggers Sørensen","Free (OFL)"),
    "cabin":("Pablo Impallari","Free (OFL)"),
    "ubuntu":("Dalton Maag","Free (OFL)"),
    "cantarell":("Dave Crossland","Free (GPL)"),
    "oxygen":("Vernon Adams","Free (OFL)"),
    "inter":("Rasmus Andersson","Free (OFL)"),
    "dm sans":("Colophon Foundry","Free (OFL)"),
    "dm serif display":("Colophon Foundry","Free (OFL)"),
    "dm mono":("Colophon Foundry","Free (OFL)"),
    "work sans":("Wei Huang","Free (OFL)"),
    "rubik":("Hubert & Fischer","Free (OFL)"),
    "nunito":("Vernon Adams","Free (OFL)"),
    "nunito sans":("Manvel Shmavonyan / Vernon Adams","Free (OFL)"),
    "figtree":("Erik Kennedy","Free (OFL)"),
    "manrope":("Mikhail Sharanda","Free (OFL)"),
    "jetbrains mono":("JetBrains","Free (OFL)"),
    "fira sans":("Erik Spiekermann / Mozilla","Free (OFL)"),
    "fira code":("Nikita Prokopov","Free (OFL)"),
    "fira mono":("Mozilla","Free (OFL)"),
    "space grotesk":("Florian Karsten","Free (OFL)"),
    "space mono":("Colophon Foundry","Free (OFL)"),
    "inconsolata":("Raph Levien / Google","Free (OFL)"),
    "libre baskerville":("Impallari Type","Free (OFL)"),
    "libre franklin":("Impallari Type","Free (OFL)"),
    "eb garamond":("Georg Duffner","Free (OFL)"),
    "crimson text":("Sebastian Kosch","Free (OFL)"),
    "cormorant garamond":("Christian Thalmann","Free (OFL)"),
    "josefin sans":("Santiago Orozco","Free (OFL)"),
    "mukta":("EkType","Free (OFL)"),
    "mulish":("Vernon Adams","Free (OFL)"),
    "karla":("Jonathan Pinhorn","Free (OFL)"),
    "lexend":("Thomas Jockin","Free (OFL)"),
    "outfit":("Rodrigo Fuenzalida","Free (OFL)"),
    "urbanist":("Corey Hu","Free (OFL)"),
    "syne":("Lucas Descroix","Free (OFL)"),
    "be vietnam pro":("Be Theme","Free (OFL)"),
    "ibm plex sans":("Bold Monday / IBM","Free (OFL)"),
    "ibm plex serif":("Bold Monday / IBM","Free (OFL)"),
    "ibm plex mono":("Bold Monday / IBM","Free (OFL)"),
    "red hat display":("MCKL","Free (OFL)"),
    "red hat text":("MCKL","Free (OFL)"),
    "system-ui":("System","OS Default"),
    "ui-sans-serif":("System","OS Default"),
    "ui-serif":("System","OS Default"),
    "ui-monospace":("System","OS Default"),
    "sans-serif":("System","OS Default"),
    "serif":("System","OS Default"),
    "monospace":("System","OS Default"),
    "cursive":("System","OS Default"),
    "fantasy":("System","OS Default"),
    "apple color emoji":("Apple","OS Default"),
    "segoe ui emoji":("Microsoft","OS Default"),
    "segoe ui symbol":("Microsoft","OS Default"),
    "noto emoji":("Google","Free (OFL)"),
    "courier":("Monotype","Restricted"),
    "icomoon":("IcoMoon","Free/Custom"),
    "font awesome 5":("Fonticons Inc.","Free/Paid"),
    "font awesome 6":("Fonticons Inc.","Free/Paid"),
    "slick":("Dafont","Paid"),
}

IGNORE = [
    "awesome","fontawesome","fa ","icomoon","glyphicon","material-icon",
    "material-symbols","fontello","feather","heroicon","linearicon","stroke-7",
    "dashicons","genericons","pe-icon","simple-line","themify","et-line",
    "flaticon","pixeden","entypo","bootstrap-icon","ionicon","socicon",
    "dearflip","flipbook","revslider","videojs","video-js",
    "vcpb","vc_grid"," vc-","swiper-icon","plugin icon","webfont icons","icon font",
]

_SKIP_FILENAME_RE = re.compile(
    r"(vcpb|vc_grid|vc-|plugin[-_]icon|icomoon|fontawesome|"
    r"glyphicons|revslider|flipbook|videojs|dashicons|themify|dearflip)",
    re.I,
)

LICENSE_FREE       = ["sil open font license","ofl","open font license","apache",
                      "mit license","bsd","creative commons","public domain",
                      "free of charge","freely available","gpl","lgpl"]
LICENSE_PAID       = ["commercial license","license fee","proprietary",
                      "all rights reserved","requires a license","purchase",
                      "subscription","annual license"]
LICENSE_RESTRICTED = ["restricted","personal use only","no commercial",
                      "non-commercial","embedding restricted","not for resale"]

# ─────────────────────────────────────────────────────────────
#  SCAN HISTORY & AI CACHE
# ─────────────────────────────────────────────────────────────
_scan_history: dict    = {}
_history_counter: int  = 0
_ai_cache: dict        = {}

def _load_ai_cache():
    global _ai_cache
    if os.path.exists(AI_CACHE_FILE):
        try:
            with open(AI_CACHE_FILE) as f:
                _ai_cache = json.load(f)
            print(f"✓ Loaded {len(_ai_cache)} cached AI font lookups")
        except Exception:
            _ai_cache = {}

def _save_ai_cache():
    try:
        with open(AI_CACHE_FILE, "w") as f:
            json.dump(_ai_cache, f, indent=2)
    except Exception:
        pass

_load_ai_cache()


# ─────────────────────────────────────────────────────────────
#  GENERAL HELPERS
# ─────────────────────────────────────────────────────────────
def normalize_font(name: str) -> str:
    if not name: return ""
    name = name.replace('"', '').replace("'", '')
    name = re.sub(
        r"\b(bold|regular|italic|light|medium|semibold|extra\s*bold|thin|"
        r"black|heavy|ultra|condensed|extended|narrow|wide|oblique|"
        r"pro|std|lt|mt|display|text|headline|caption)\b",
        "", name, flags=re.I,
    )
    name = re.sub(r"[-_\s]*\d+$", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip().lower()


def is_text_font(name: str) -> bool:
    n = normalize_font(name)
    return bool(n) and not any(i in n for i in IGNORE)


def detect_from_cdn(url: str):
    url_l = url.lower()
    for pat, foundry, lic in CDN_PATTERNS:
        if pat in url_l:
            return foundry, lic
    return None, None


def parse_license(text: str):
    if not text: return None
    t = text.lower()
    for kw in LICENSE_FREE:
        if kw in t:
            if "ofl" in t or "open font" in t: return "Free (OFL)"
            if "apache" in t: return "Free (Apache)"
            if "mit" in t:    return "Free (MIT)"
            if "gpl" in t:    return "Free (GPL)"
            return "Free"
    for kw in LICENSE_RESTRICTED:
        if kw in t: return "Restricted"
    for kw in LICENSE_PAID:
        if kw in t: return "Paid"
    return None


def lookup_db(font_key: str):
    key = normalize_font(font_key)
    if key in FONT_DB: return FONT_DB[key]
    for k, v in FONT_DB.items():
        if k and len(k) >= 4 and (k in key or key in k):
            return v
    return None


# ─────────────────────────────────────────────────────────────
#  PATH HELPERS
# ─────────────────────────────────────────────────────────────
def make_font_file_path(filename: str) -> str:
    return f"Inspect > Application > Frames > Top > Font > {filename}"

def make_css_path(sheet_href: str) -> str:
    filename = os.path.basename(urlparse(sheet_href).path) or sheet_href
    return f"Inspect > Application > Frames > Top > css > {filename}"


# ─────────────────────────────────────────────────────────────
#  SERVER-SIDE CSS PARSER  (Method 5)
# ─────────────────────────────────────────────────────────────
_FONT_FACE_BLOCK_RE = re.compile(r"@font-face\s*\{([^}]+)\}", re.IGNORECASE | re.DOTALL)
_FF_VALUE_RE        = re.compile(r"font-family\s*:\s*([^;}\n]+)", re.IGNORECASE)
_SRC_URL_RE         = re.compile(r'url\(\s*["\']?([^"\')\s]+)["\']?\s*\)', re.IGNORECASE)
_RULE_BLOCK_RE      = re.compile(r"[^{]+\{([^}]+)\}", re.DOTALL)
_COMMENT_RE         = re.compile(r"/\*.*?\*/", re.DOTALL)

_CSS_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": "text/css,*/*;q=0.1",
}


def _parse_css_server_side(css_url: str) -> list:
    results: list = []
    try:
        r = req_lib.get(css_url, timeout=12, headers={**_CSS_FETCH_HEADERS, "Referer": css_url})
        if r.status_code != 200:
            print(f"  [CSS-FETCH {r.status_code}] {css_url}")
            return results
    except Exception as e:
        print(f"  [CSS-FETCH err] {css_url}: {e}")
        return results

    css_text = _COMMENT_RE.sub("", r.text)

    # Pass A: @font-face blocks
    for block_match in _FONT_FACE_BLOCK_RE.finditer(css_text):
        block = block_match.group(1)
        ff_m  = _FF_VALUE_RE.search(block)
        if not ff_m: continue
        raw_family = ff_m.group(1).strip().strip("'\"").strip()
        if not raw_family or not is_text_font(normalize_font(raw_family)): continue

        src_m   = _SRC_URL_RE.search(block)
        src_url = src_m.group(1).strip() if src_m else ""

        if src_url and not src_url.startswith(("http://", "https://", "//")):
            base    = css_url.rsplit("/", 1)[0]
            src_url = base + "/" + src_url.lstrip("./")
        if src_url.startswith("//"):
            src_url = "https:" + src_url

        foundry, lic = None, None
        if src_url.startswith("http"):
            foundry, lic = detect_from_cdn(src_url)

        results.append({
            "display": raw_family, "family": raw_family,
            "foundry": foundry,    "license": lic,
            "src_url": src_url,    "kind":    "font-face-css",
        })

    # Pass B: font-family in all other rules
    css_no_ff    = _FONT_FACE_BLOCK_RE.sub("", css_text)
    seen_in_file = {normalize_font(r["display"]) for r in results}

    for rule_match in _RULE_BLOCK_RE.finditer(css_no_ff):
        block = rule_match.group(1)
        for ff_m in _FF_VALUE_RE.finditer(block):
            raw_val = ff_m.group(1).strip()
            for part in raw_val.split(","):
                raw_name = part.strip().strip("'\"").strip()
                if not raw_name: continue
                nname = normalize_font(raw_name)
                if not is_text_font(nname) or nname in seen_in_file: continue
                seen_in_file.add(nname)
                results.append({
                    "display": raw_name, "family": raw_name,
                    "foundry": None,     "license": None,
                    "src_url": "",       "kind":    "css-decl",
                })

    return results


# ─────────────────────────────────────────────────────────────
#  AI FONT LOOKUP (OpenAI)
# ─────────────────────────────────────────────────────────────
_AI_SYSTEM_PROMPT = """\
You are the world's foremost authority on typography and font licensing with 30+ years of
encyclopedic knowledge across every typeface ever commercially released or freely distributed.

RULES (never break):
1. NEVER output "Unknown" for foundry — always provide the real foundry or your best-informed answer.
2. NEVER output "Check manually" for license — always state the precise license.
3. license must be exactly one of: Free (OFL) | Free (Apache) | Free (MIT) | Free (GPL) |
   Free | Paid | Restricted | OS Default | Free/Custom
4. Icon/symbol fonts: set license = "Icon/Symbol — skip"
5. System fonts bundled with OS: OS Default
6. Google Fonts = Free (OFL) unless you know otherwise
7. Adobe Fonts / Typekit = Paid
8. confidence: "high" | "medium" | "low"

Respond ONLY with a valid JSON object — no markdown, no extra text.\
"""

_AI_USER_TEMPLATE = """\
Font name: "{name}"
{context}
Return JSON with keys: foundry, designer, license, license_detail, year, category, confidence
"""

def lookup_ai(font_name: str, extra_context: str = "", notify=None) -> dict | None:
    api_key = OPENAI_API_KEY
    if not api_key or api_key.strip() in ("", "sk-YOUR-KEY-HERE", "YOUR_OPENAI_API_KEY"):
        return None

    ck = normalize_font(font_name)
    if ck in _ai_cache and not extra_context:
        cached = _ai_cache[ck]
        if not _is_unknown(cached.get("foundry", "")) and not _is_unknown(cached.get("license", "")):
            return cached

    ctx      = f"Additional context: {extra_context}" if extra_context else ""
    user_msg = _AI_USER_TEMPLATE.format(name=font_name, context=ctx)

    def _evt(type_, **kw):
        return f"data: {json.dumps({'type': type_, 'ts': datetime.datetime.now().isoformat(), **kw})}\n\n"

    if notify:
        notify(_evt("ai_call", font=font_name, model=OPENAI_MODEL))

    try:
        r = req_lib.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model":           OPENAI_MODEL,
                "messages":        [
                    {"role": "system", "content": _AI_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                "temperature":     0,
                "max_tokens":      300,
                "response_format": {"type": "json_object"},
            },
            timeout=25,
        )

        if r.status_code != 200:
            print(f"⚠ OpenAI {r.status_code}: {r.text[:300]}")
            if notify: notify(_evt("ai_error", font=font_name, status=r.status_code))
            return None

        raw  = r.json()["choices"][0]["message"]["content"].strip()
        data = json.loads(raw)
        data["source"] = "ai"

        if not _is_unknown(data.get("foundry", "")) and not _is_unknown(data.get("license", "")):
            _ai_cache[ck] = data
            _save_ai_cache()

        confidence = data.get("confidence", "high")
        icon = "🟢" if confidence == "high" else ("🟡" if confidence == "medium" else "🔴")
        print(f"🤖{icon} AI → {font_name}: {data.get('foundry')} | {data.get('license')}")

        if notify:
            notify(_evt("ai_done",
                        font=font_name,
                        foundry=data.get("foundry", ""),
                        license=data.get("license", ""),
                        confidence=confidence))
        return data

    except json.JSONDecodeError as e:
        print(f"✗ AI JSON parse error for '{font_name}': {e}")
        return None
    except Exception as e:
        print(f"✗ AI lookup failed for '{font_name}': {e}")
        return None


# ─────────────────────────────────────────────────────────────
#  FONT FILE PARSER
# ─────────────────────────────────────────────────────────────
def get_font_info_from_file(font_url: str):
    cdn_f, cdn_l = detect_from_cdn(font_url)
    try:
        r = req_lib.get(font_url, timeout=15)
        if r.status_code != 200:
            return None, None, cdn_f, cdn_l
        font  = TTFont(BytesIO(r.content))
        names = font["name"].names

        def _g(nid):
            for rec in names:
                if rec.nameID == nid:
                    try:
                        val = rec.toUnicode().strip()
                        if val: return val
                    except Exception:
                        try:
                            val = rec.string.decode("latin-1", errors="ignore").strip()
                            if val: return val
                        except Exception:
                            pass
            return None

        full_name   = _g(4)
        family_name = _g(16) or _g(1)
        subfamily   = _g(2) or ""

        if not full_name and not family_name:
            return None, None, cdn_f, cdn_l

        if full_name:
            display_name = full_name
        elif subfamily and subfamily.lower() not in ("regular", ""):
            display_name = f"{family_name} {subfamily}".strip()
        else:
            display_name = family_name

        _icon_kw = ["icon","symbol","glyph","pictogram","awesome","icomoon",
                    "fontello","feather","ionicon","vcpb","vc_grid","plugin",
                    "slick","revslider","dashicon"]
        if any(k in display_name.lower() for k in _icon_kw):
            return None, None, cdn_f, cdn_l

        _WEIGHT_PAT = re.compile(
            r"\s+(Regular|Bold|Italic|Light|Medium|SemiBold|Semi\s*Bold|"
            r"ExtraBold|Extra\s*Bold|Thin|Black|Heavy|Ultra|Condensed|"
            r"Extended|Narrow|Wide|Oblique|Roman|Book|Demi|ExtraLight)$",
            re.I,
        )
        if family_name:
            family_name = _WEIGHT_PAT.sub("", family_name).strip()

        mfg = _g(8) or _g(9)
        if not mfg:
            vu = _g(11)
            if vu:
                try: mfg = urlparse(vu).netloc.replace("www.", "") or None
                except: pass

        foundry = cdn_f or mfg
        lic     = cdn_l if cdn_l else (parse_license(_g(13)) or parse_license(_g(14)))
        return display_name, family_name, foundry, lic

    except Exception as e:
        print(f"Font file parse error for {font_url}: {e}")
        return None, None, cdn_f, cdn_l


# ─────────────────────────────────────────────────────────────
#  RESOLVE  — pull together all sources of font intelligence
# ─────────────────────────────────────────────────────────────
def _resolve(font_key, file_foundry=None, file_lic=None, use_ai=True, notify=None):
    designer   = ""
    lic_detail = ""
    confidence = "high"

    if file_foundry and file_lic and not _is_unknown(file_foundry) and not _is_unknown(file_lic):
        if use_ai:
            ai = lookup_ai(font_key, notify=notify)
            if ai:
                designer   = ai.get("designer", "")
                lic_detail = ai.get("license_detail", "")
                confidence = ai.get("confidence", "high")
        return file_foundry, file_lic, "file", designer, lic_detail, confidence

    db = lookup_db(font_key)

    if db and not _is_unknown(db[0]) and not _is_unknown(db[1]):
        foundry = file_foundry if (file_foundry and not _is_unknown(file_foundry)) else db[0]
        lic     = file_lic     if (file_lic     and not _is_unknown(file_lic))     else db[1]
        src     = "file+db" if file_foundry else "db"
        if use_ai:
            ai = lookup_ai(font_key, notify=notify)
            if ai:
                designer   = ai.get("designer", "")
                lic_detail = ai.get("license_detail", "")
                confidence = ai.get("confidence", "high")
        return foundry, lic, src, designer, lic_detail, confidence

    if use_ai:
        ctx = f"File foundry hint: {file_foundry}" if file_foundry else ""
        ai  = lookup_ai(font_key, extra_context=ctx, notify=notify)
        if ai:
            foundry = ai.get("foundry", "")
            lic     = ai.get("license", "")
            if _is_unknown(foundry): foundry = f"{font_key.title()} (foundry unconfirmed)"
            if _is_unknown(lic):     lic     = "Check license manually"
            return (foundry, lic, "ai",
                    ai.get("designer", ""), ai.get("license_detail", ""),
                    ai.get("confidence", "low"))

    foundry = file_foundry or (db[0] if db else f"{font_key.title()} (foundry unconfirmed)")
    lic     = file_lic     or (db[1] if db else "License unconfirmed — enable AI")
    return foundry, lic, "—", "", "", "low"


# ─────────────────────────────────────────────────────────────
#  CORE SCANNER
# ─────────────────────────────────────────────────────────────
def _blocking_scan(url, wait_sec, scroll_steps, use_ai, queue, scan_id):

    def emit_event(type_, **kw):
        return f"data: {json.dumps({'type': type_, 'ts': datetime.datetime.now().isoformat(), **kw})}\n\n"

    def push(s):
        queue.put_nowait(s)

    _WEIGHT_RE = re.compile(
        r"\s*(Bold|Italic|Light|Thin|Medium|Semi\s*Bold|Extra\s*Bold|"
        r"Black|Heavy|Ultra|Condensed|Narrow|Wide|Extended|Oblique|"
        r"Regular|Roman|Book|Demi|ExtraLight|Extra\s*Light).*$",
        re.I,
    )

    def family_key(display_name: str) -> str:
        clean = _WEIGHT_RE.sub("", display_name or "").strip()
        return normalize_font(clean) if clean else normalize_font(display_name or "")

    seen_files   = set()
    seen_names   = set()
    emb_families = set()
    font_list    = []

    def record(fd):
        font_list.append(fd)
        if scan_id in _scan_history:
            _scan_history[scan_id]["fonts"] = font_list.copy()

    def emit_font(fd):
        """
        Route font based on license:
          OS Default / Icon-Symbol  → suppress entirely
          Restricted                → pass through tagged as is_restricted=True
          Free / Paid               → normalise and emit normally
        """
        raw_lic = fd.get("license", "")
        l       = raw_lic.strip().lower()

        # Hard suppress
        if l in ("os default",) or "os default" in l or "icon/symbol" in l:
            return

        # Font type from kind
        fd["font_type"] = classify_font_type(fd.get("kind", ""))

        # Restricted → separate section
        if "restricted" in l:
            fd["is_restricted"] = True
            record(fd)
            push(emit_event("font", **fd))
            return

        # Normalise Free variants
        if l.startswith("free"):
            fd["license"] = "Free"
        elif "paid" in l:
            fd["license"] = "Free/Paid" if l.startswith("free") else "Paid"
        else:
            # Truly unknown → suppress
            print(f"  [SUPPRESS unknown] {fd.get('name')} → {raw_lic}")
            return

        fd["is_restricted"] = False
        record(fd)
        push(emit_event("font", **fd))

    def make_fd(display: str, family: str, fkey: str,
                file_foundry, file_lic, path: str, kind: str) -> dict:
        """Build a complete font descriptor dict."""
        foundry, lic, src, designer, lic_detail, conf = _resolve(
            fkey, file_foundry, file_lic, use_ai, notify=push)
        return dict(
            site_url=url,
            name=display,
            family=family or display,
            foundry=foundry,
            license=lic,
            path=path,
            source=src,
            kind=kind,
            font_type=classify_font_type(kind),   # pre-set; emit_font may override
            is_restricted=False,
            designer=designer,
            license_detail=lic_detail,
            confidence=conf,
        )

    push(emit_event("status", message="Starting stealth browser…"))

    opts = uc.ChromeOptions()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1440,900")
    opts.add_argument("--log-level=3")
    opts.add_argument("--silent")
    opts.add_argument("--remote-debugging-port=0")
    opts.add_argument("--disable-web-security")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
    opts.add_argument("--lang=en-US,en;q=0.9")
    opts.add_argument("--disable-blink-features=AutomationControlled")

    try:
        driver = uc.Chrome(options=opts, use_subprocess=True, version_main=145)
    except WebDriverException as e:
        push(emit_event("error", message=f"ChromeDriver failed: {e}"))
        queue.put_nowait(None)
        return

    import time as _time
    _time.sleep(0.4)

    try:
        stealth(driver, languages=["en-US","en"], vendor="Google Inc.",
                platform="Win32", webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine", fix_hairline=True)
    except NoSuchWindowException:
        push(emit_event("error", message="Chrome window closed before stealth patches applied."))
        try: driver.quit()
        except: pass
        queue.put_nowait(None)
        return
    except Exception as e:
        print(f"⚠ stealth() failed (non-fatal): {e}")

    _JS_IDLE = """
        try {
            if (document.readyState !== 'complete') return false;
            const entries = performance.getEntriesByType('resource');
            const now = performance.now();
            return entries.filter(e => e.responseEnd === 0 ||
                (now - e.responseEnd) < 500).length === 0;
        } catch(e) { return true; }
    """

    try:
        push(emit_event("status", message=f"Loading {url}…"))
        driver.get(url)

        try:
            WebDriverWait(driver, max(wait_sec, 15)).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            push(emit_event("error", message="Page did not load"))
            return

        try:
            WebDriverWait(driver, max(wait_sec, 10)).until(
                lambda d: d.execute_script(_JS_IDLE)
            )
        except TimeoutException:
            pass

        try:
            for _ in range(max(0, scroll_steps)):
                prev_y = driver.execute_script("return window.scrollY;")
                driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
                try:
                    WebDriverWait(driver, 1.5).until(
                        lambda d, py=prev_y: d.execute_script("return window.scrollY;") != py
                    )
                except TimeoutException:
                    pass
                try:
                    WebDriverWait(driver, 0.8).until(lambda d: d.execute_script(_JS_IDLE))
                except TimeoutException:
                    pass
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

        # ── Method 1: Actual font file resources ──────────────
        push(emit_event("status", message="Method 1 — Scanning font file resources…"))
        try:
            resources = driver.execute_script("""
                try {
                    return performance.getEntriesByType("resource")
                        .filter(r => r && r.name &&
                            r.name.match(/\\.(woff2?|ttf|otf)(\\?|#|$)/i))
                        .map(r => r.name);
                } catch(e) { return []; }
            """) or []
        except Exception:
            resources = []

        for res_url in resources:
            filename  = os.path.basename(urlparse(res_url).path)
            fkey_file = filename.lower()
            if fkey_file in seen_files: continue
            if _SKIP_FILENAME_RE.search(filename):
                seen_files.add(fkey_file)
                continue
            seen_files.add(fkey_file)

            display_name, family_name, file_foundry, file_lic = get_font_info_from_file(res_url)
            if not display_name: continue

            display_name = display_name.strip()
            fkey = family_key(family_name or display_name)
            if not is_text_font(fkey) or not is_text_font(normalize_font(display_name)): continue

            emb_families.add(fkey)
            seen_names.add(normalize_font(display_name))
            path = make_font_file_path(filename)
            fd   = make_fd(display_name, family_name or display_name,
                           fkey, file_foundry, file_lic, path, "embedded")
            emit_font(fd)
            print(f"  [FILE] {display_name}")

        push(emit_event("status", message=f"  → {len(font_list)} fonts from files"))

        # ── Method 2: @font-face from CSS files ───────────────
        push(emit_event("status", message="Method 2 — Parsing @font-face from CSS files…"))

        font_faces = driver.execute_script("""
            const result = [];
            function processSS(ss, sheetHref) {
                try {
                    for (const rule of ss.cssRules || []) {
                        if (rule.type === 5) {
                            const family = (rule.style.fontFamily || '').replace(/['"]/g,'').trim();
                            const style  = (rule.style.fontStyle  || 'normal').trim();
                            const weight = (rule.style.fontWeight || 'normal').trim();
                            const src    = rule.style.src || '';
                            if (family) result.push({family, style, weight, src, sheet: sheetHref});
                        }
                        if (rule.cssRules) { try { processSS(rule, sheetHref); } catch(e) {} }
                    }
                } catch(e) {}
            }
            try {
                for (const ss of document.styleSheets) {
                    if (ss.href && ss.href !== '__inline__') processSS(ss, ss.href);
                }
            } catch(e) {}
            return result;
        """) or []

        for ff in font_faces:
            family  = (ff.get("family") or "").strip()
            style   = (ff.get("style")  or "normal").strip()
            weight  = (ff.get("weight") or "normal").strip()
            src_str = ff.get("src", "")
            sheet   = ff.get("sheet", "")

            if not sheet or sheet == "__inline__": continue
            if not family or not is_text_font(normalize_font(family)): continue

            w_map = {"100":"Thin","200":"ExtraLight","300":"Light","400":"Regular",
                     "500":"Medium","600":"SemiBold","700":"Bold","800":"ExtraBold","900":"Black"}
            wlabel  = w_map.get(weight, weight if weight not in ("normal", "400") else "")
            slabel  = "Italic" if "italic" in style.lower() else ""
            suffix  = " ".join(filter(None, [wlabel, slabel]))
            display = f"{family} {suffix}".strip() if (suffix and suffix.lower() not in family.lower()) else family

            dn   = normalize_font(display)
            fkey = normalize_font(family)
            if dn in seen_names: continue
            seen_names.add(dn)

            file_foundry, file_lic = None, None
            url_match = re.search(r'url\(["\']?([^"\')\s]+)["\']?\)', src_str)
            if url_match:
                furl = url_match.group(1)
                if furl.startswith("http"):
                    _, _, file_foundry, file_lic = get_font_info_from_file(furl)

            emb_families.add(fkey)
            path = make_css_path(sheet)
            fd   = make_fd(display, family, fkey, file_foundry, file_lic, path, "font-face")
            emit_font(fd)
            print(f"  [@font-face] {display}")

        push(emit_event("status", message=f"  → {len(font_list)} fonts total so far"))

        # ── Method 3: Google Fonts <link> tags ────────────────
        push(emit_event("status", message="Method 3 — Checking Google Fonts links…"))

        gf_links = driver.execute_script("""
            const links = [];
            document.querySelectorAll('link[href*="fonts.googleapis.com"]').forEach(l => links.push(l.href));
            try {
                for (const ss of document.styleSheets) {
                    try {
                        for (const r of ss.cssRules) {
                            if (r.type === 3 && r.href && r.href.includes('fonts.googleapis.com'))
                                links.push(r.href);
                        }
                    } catch(e) {}
                }
            } catch(e) {}
            return [...new Set(links)];
        """) or []

        for gf_url in gf_links:
            try:
                parsed         = urlparse(gf_url)
                qs             = parse_qs(parsed.query)
                families_param = qs.get("family", [])
                for fp in families_param:
                    for fpart in fp.split("|"):
                        fname_raw = fpart.split(":")[0].replace("+", " ").strip()
                        axes_part = fpart[len(fpart.split(":")[0]):].lstrip(":")
                        if not fname_raw: continue

                        variants = []
                        if "wght@" in axes_part or "ital,wght@" in axes_part:
                            has_ital = "ital" in axes_part
                            nums = re.findall(r"[\d]+", axes_part)
                            if has_ital and len(nums) >= 2:
                                pairs = list(zip(nums[::2], nums[1::2]))
                                for ital, wght in pairs:
                                    variants.append((ital == "1", wght))
                            else:
                                for w in nums:
                                    if int(w) >= 100:
                                        variants.append((False, w))
                        if not variants:
                            variants = [(False, "400")]

                        w_map2 = {"100":"Thin","200":"ExtraLight","300":"Light",
                                  "400":"Regular","500":"Medium","600":"SemiBold",
                                  "700":"Bold","800":"ExtraBold","900":"Black"}
                        for is_italic, wght in variants:
                            wlabel  = w_map2.get(str(wght), f"W{wght}")
                            display = fname_raw
                            if wlabel and wlabel != "Regular": display += f" {wlabel}"
                            if is_italic: display += " Italic"

                            dn   = normalize_font(display)
                            fkey = normalize_font(fname_raw)
                            if dn in seen_names: continue
                            seen_names.add(dn)
                            emb_families.add(fkey)
                            path = make_css_path("fonts.googleapis.com")
                            fd   = make_fd(display, fname_raw, fkey,
                                           "Google", "Free (OFL)", path, "google-fonts")
                            emit_font(fd)
                            print(f"  [GF] {display}")
            except Exception as ex:
                print(f"Google Fonts parse error: {ex}")

        # ── Method 4: <link rel=preload as=font> ──────────────
        push(emit_event("status", message="Method 4 — Checking preloaded fonts…"))

        preloads = driver.execute_script("""
            return Array.from(
                document.querySelectorAll('link[rel="preload"][as="font"]')
            ).map(l => l.href).filter(Boolean);
        """) or []

        for pre_url in preloads:
            filename  = os.path.basename(urlparse(pre_url).path)
            fkey_file = filename.lower()
            if fkey_file in seen_files: continue
            if _SKIP_FILENAME_RE.search(filename):
                seen_files.add(fkey_file)
                continue
            seen_files.add(fkey_file)

            display_name, family_name, file_foundry, file_lic = get_font_info_from_file(pre_url)
            if not display_name: continue

            fkey = family_key(family_name or display_name)
            dn   = normalize_font(display_name)
            if not is_text_font(fkey) or dn in seen_names: continue

            seen_names.add(dn)
            emb_families.add(fkey)
            path = make_font_file_path(filename)
            fd   = make_fd(display_name, family_name or display_name,
                           fkey, file_foundry, file_lic, path, "preload")
            emit_font(fd)
            print(f"  [PRELOAD] {display_name}")

        # ── Method 5: Server-side CSS file parsing ────────────
        push(emit_event("status", message="Method 5 — Server-side CSS parsing…"))

        try:
            css_resource_urls = driver.execute_script("""
                try {
                    return performance.getEntriesByType("resource")
                        .filter(r => r && r.name &&
                            r.name.match(/\\.css(\\?|#|$)/i) &&
                            !r.name.includes('fonts.googleapis.com'))
                        .map(r => r.name);
                } catch(e) { return []; }
            """) or []
        except Exception:
            css_resource_urls = []

        seen_css_urls: set  = set()
        unique_css_urls: list = []
        for cu in css_resource_urls:
            key = urlparse(cu).path.lower()
            if key not in seen_css_urls:
                seen_css_urls.add(key)
                unique_css_urls.append(cu)

        print(f"  [CSS-FETCH] {len(unique_css_urls)} unique CSS files")

        for css_url in unique_css_urls:
            css_filename = os.path.basename(urlparse(css_url).path) or "stylesheet.css"
            push(emit_event("status", message=f"  Parsing {css_filename}…"))

            for entry in _parse_css_server_side(css_url):
                display  = entry["display"].strip()
                family   = entry["family"].strip()
                fkey     = normalize_font(family)
                dn       = normalize_font(display)

                if dn in seen_names or fkey in emb_families: continue
                if not is_text_font(fkey): continue

                seen_names.add(dn)

                file_foundry = entry.get("foundry")
                file_lic     = entry.get("license")
                src_url      = entry.get("src_url", "")

                if src_url and src_url.startswith("http") and not file_foundry:
                    _, _, file_foundry, file_lic = get_font_info_from_file(src_url)

                path = make_css_path(css_url)
                fd   = make_fd(display, family, fkey, file_foundry, file_lic,
                               path, entry["kind"])
                emit_font(fd)

        push(emit_event("status", message=f"  → {len(font_list)} fonts total after CSS parsing"))

        # ── Done ──────────────────────────────────────────────
        total      = len(font_list)
        ai_count   = sum(1 for f in font_list if f.get("source") == "ai")
        restr_count = sum(1 for f in font_list if f.get("is_restricted"))

        if scan_id in _scan_history:
            _scan_history[scan_id].update({
                "fonts": font_list.copy(), "total": total, "done": True,
                "restricted_count": restr_count,
            })

        push(emit_event("done", total=total, scan_id=scan_id,
                        ai_resolved=ai_count, restricted=restr_count,
                        message=f"{total} fonts found ({ai_count} via AI, {restr_count} restricted)"))
        print(f"\n✓ Scan complete: {total} fonts ({ai_count} AI, {restr_count} restricted)")

    except Exception as e:
        import traceback
        push(emit_event("error", message=str(e)))
        print(f"Scanner error: {traceback.format_exc()}")
    finally:
        try: driver.quit()
        except: pass
        queue.put_nowait(None)


# ─────────────────────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="FontScan API — Enhanced")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


@app.get("/")
def serve_frontend():
    return FileResponse("index.html") if os.path.exists("index.html") \
        else HTMLResponse("<h2>Place index.html next to backend.py</h2>")


@app.get("/api/scan")
async def scan_endpoint(
    url:    str  = Query(...),
    wait:   int  = Query(8),
    scroll: int  = Query(3),
    use_ai: bool = Query(True),
):
    global _history_counter
    if not url.startswith("http"):
        url = "https://" + url

    _history_counter += 1
    scan_id = f"scan_{_history_counter}"
    domain  = urlparse(url).netloc

    _scan_history[scan_id] = {
        "scan_id": scan_id, "url": url, "domain": domain,
        "ts":    datetime.datetime.now().strftime("%d %b %Y, %H:%M"),
        "fonts": [], "total": 0, "done": False, "restricted_count": 0,
    }
    if len(_scan_history) > 20:
        del _scan_history[next(iter(_scan_history))]

    queue: asyncio.Queue = asyncio.Queue()
    loop  = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _blocking_scan,
                         url, wait, scroll, use_ai, queue, scan_id)

    async def event_stream():
        while True:
            item = await queue.get()
            if item is None: break
            yield item

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/estimate-traffic")
async def estimate_traffic_endpoint(url: str = Query(...)):
    """
    Estimate monthly website traffic using GPT-4o.

    Returns: { "url": str, "domain": str, "estimate": str, "cached": bool }

    Example response:
      { "url": "https://stripe.com", "domain": "stripe.com",
        "estimate": "4.5M", "cached": false }
    """
    if not url.startswith("http"):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
    except Exception:
        domain = url

    cache_key = domain.lower()
    cached    = cache_key in _traffic_cache

    loop     = asyncio.get_event_loop()
    estimate = await loop.run_in_executor(_executor, estimate_monthly_traffic, url)

    return {
        "url":      url,
        "domain":   domain,
        "estimate": estimate,
        "cached":   cached,
    }


@app.get("/api/history")
def get_history():
    items = list(reversed(list(_scan_history.values())))
    return {"history": [{k: v for k, v in h.items() if k != "fonts"} for h in items]}


@app.get("/api/history/{scan_id}")
def get_history_detail(scan_id: str):
    h = _scan_history.get(scan_id)
    if not h:
        raise HTTPException(status_code=404, detail="Scan not found")
    return h


@app.get("/api/health")
def health():
    ai_ok = bool(OPENAI_API_KEY) and OPENAI_API_KEY.strip() not in ("", "sk-YOUR-KEY-HERE")
    return {
        "status":               "ok",
        "cached_ai_lookups":    len(_ai_cache),
        "cached_traffic_est":   len(_traffic_cache),
        "scans_in_memory":      len(_scan_history),
        "ai_configured":        ai_ok,
        "model":                OPENAI_MODEL,
    }


if __name__ == "__main__":
    print("─" * 56)
    print("  FontScan Enhanced  →  http://localhost:8000")
    ai_ok = OPENAI_API_KEY and OPENAI_API_KEY.strip() not in ("", "sk-YOUR-KEY-HERE")
    print(f"  OpenAI ({OPENAI_MODEL}): {'✓ configured' if ai_ok else '✗ NOT SET'}")
    print(f"  Font AI cache:     {len(_ai_cache)} entries")
    print(f"  Traffic cache:     {len(_traffic_cache)} entries")
    print(f"  Chrome:            pinned to v145")
    print(f"  Features:          Traffic Est · Font Type · Restricted Section")
    print("─" * 56)
    uvicorn.run("backend:app", host="0.0.0.0", port=8000,
                reload=False, log_level="warning")