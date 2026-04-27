"""OPENAI_API_KEY
FontScan – Enhanced Backend + JWT Authentication
Run: python backend.py  →  http://localhost:8000
"""

import re, os, json, datetime, asyncio, concurrent.futures
import requests as req_lib
from io import BytesIO
from urllib.parse import urlparse, parse_qs, urljoin
from fastapi import FastAPI, Query, HTTPException, Depends, Header
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import uvicorn
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from fontTools.ttLib import TTFont
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Playwright browser path (set by Render env var)
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/opt/render/project/src/.playwright-browsers"
)
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

# ── Auth imports ─────────────────────────────────────────────
from auth import (
    User, UserCreate, UserLogin, Token, UserOut,
    get_db, hash_password, verify_password, create_access_token,
    get_current_user, require_admin, init_default_admin, verify_token_param,
)

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = "gpt-4o"
AI_CACHE_FILE  = "font_ai_cache.json"

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
    css_kinds = {"google-fonts", "css-decl"}
    return "CSS Font" if kind in css_kinds else "Embedded Font"


# ─────────────────────────────────────────────────────────────
#  TRAFFIC  —  SimilarWeb primary · GPT-4o fallback
#  NO CACHING — every call fetches a fresh live number
# ─────────────────────────────────────────────────────────────

def format_traffic(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{round(n / 1_000_000_000, 1)}B"
    if n >= 1_000_000:
        return f"{round(n / 1_000_000, 1)}M"
    if n >= 1_000:
        return f"{round(n / 1_000, 1)}K"
    return str(n)


_SW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua":         '"Chromium";v="147", "Google Chrome";v="147", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":  "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":  "empty",
    "sec-fetch-mode":  "cors",
    "sec-fetch-site":  "same-site",
    "Connection":      "keep-alive",
}


def get_similarweb_traffic(domain: str) -> tuple:
    clean = domain.lower().replace("www.", "").strip("/")
    sw_api = f"https://data.similarweb.com/api/v1/data?domain={clean}"
    headers = {
        **_SW_HEADERS,
        "Origin":  "https://www.similarweb.com",
        "Referer": f"https://www.similarweb.com/website/{clean}/",
    }
    try:
        r = req_lib.get(sw_api, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"  [SW] HTTP {r.status_code} for {domain}")
            r2 = req_lib.get(
                f"https://data.similarweb.com/api/v1/data?domain={clean}",
                headers={**headers, "Referer": "https://www.google.com/"},
                timeout=20,
            )
            if r2.status_code != 200:
                return None, None
            r = r2

        data = r.json()
        raw_visits = None

        for eng_key in ("Engagments", "Engagements"):
            eng = data.get(eng_key) or {}
            v   = eng.get("Visits", "")
            if v:
                try:
                    raw_visits = int(float(str(v).replace(",", "")))
                    break
                except (ValueError, TypeError):
                    pass

        if not raw_visits:
            emv = data.get("EstimatedMonthlyVisits") or {}
            if emv:
                latest_key = sorted(emv.keys())[-1]
                try:
                    raw_visits = int(float(str(emv[latest_key]).replace(",", "")))
                except (ValueError, TypeError):
                    pass

        if raw_visits and raw_visits > 0:
            formatted = format_traffic(raw_visits)
            print(f"📊 SimilarWeb ✓ → {domain}: {formatted} ({raw_visits:,}/mo)")
            return raw_visits, formatted

        print(f"  [SW] No visit data in response for {domain}")
        return None, None

    except json.JSONDecodeError as e:
        print(f"  [SW] JSON error for {domain}: {e}")
    except Exception as e:
        print(f"  [SW] Request error for {domain}: {e}")

    return None, None


_TRAFFIC_PROMPT = """\
You are a professional web traffic analyst with expertise equivalent to SimilarWeb.

Estimate the MONTHLY WEBSITE VISITS for the following URL:

URL     : {url}
Domain  : {domain}
TLD     : {tld}

Calibration:
- google.com / youtube.com       : 30B–90B / month
- amazon.com / wikipedia.org     : 2B–5B   / month
- Major news sites (bbc, nytimes): 200M–500M / month
- Popular SaaS / dev tools       : 5M–50M  / month
- Regional brands / mid e-comm   : 500K–5M / month
- Small business / niche blogs   : 50K–500K / month
- Local or new sites             : 5K–50K  / month
- Very niche / micro sites       : under 5K / month

Return ONLY a single plain integer. No commas, no text, no symbols.
Examples:  45000000   1250000   87000   12000
"""


def _estimate_traffic_gpt(url: str, domain: str, tld: str) -> str:
    api_key = OPENAI_API_KEY
    if not api_key or api_key.strip() in ("", "sk-YOUR-KEY-HERE"):
        return "N/A"
    prompt = _TRAFFIC_PROMPT.format(url=url, domain=domain, tld=tld)
    try:
        r = req_lib.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": OPENAI_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0, "max_tokens": 20},
            timeout=20,
        )
        if r.status_code != 200:
            return "N/A"
        raw_text = r.json()["choices"][0]["message"]["content"].strip()
        match    = re.search(r"\d[\d,]*", raw_text)
        if not match:
            return "N/A"
        raw_n     = int(match.group().replace(",", ""))
        formatted = format_traffic(raw_n)
        print(f"📊 GPT estimate (fresh) → {domain}: {formatted} ({raw_n:,}/mo)")
        return formatted
    except Exception as e:
        print(f"✗ GPT traffic estimate failed for {url}: {e}")
        return "N/A"


def estimate_monthly_traffic(url: str) -> str:
    if not url.startswith("http"):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        tld    = domain.split(".")[-1] if "." in domain else "com"
    except Exception:
        domain, tld = url, "com"
    _, sw_formatted = get_similarweb_traffic(domain)
    if sw_formatted:
        return sw_formatted
    print(f"  [SW] Falling back to GPT for {domain}")
    return _estimate_traffic_gpt(url, domain, tld)


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
    "avenir":("Linetype","Paid"),
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
_scan_history: dict   = {}
_history_counter: int = 0
_ai_cache: dict       = {}

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
#  GENERAL HELPERS  (unchanged from original)
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

def make_font_file_path(filename: str) -> str:
    return f"Inspect > Application > Frames > Top > Font > {filename}"

def make_css_path(sheet_href: str) -> str:
    filename = os.path.basename(urlparse(sheet_href).path) or sheet_href
    return f"Inspect > Application > Frames > Top > css > {filename}"


# ─────────────────────────────────────────────────────────────
#  SERVER-SIDE CSS PARSER  (Method 5)  — unchanged from original
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
        "Chrome/147.0.0.0 Safari/537.36"
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
#  AI FONT LOOKUP (OpenAI)  — unchanged from original
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
#  FONT FILE PARSER  — unchanged from original
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
#  RESOLVE  — unchanged from original
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
#  CORE SCANNER  — unchanged from original
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
        raw_lic = fd.get("license", "")
        l       = raw_lic.strip().lower()

        if l in ("os default",) or "os default" in l or "icon/symbol" in l:
            return

        fd["font_type"] = classify_font_type(fd.get("kind", ""))

        if "restricted" in l:
            fd["is_restricted"] = True
            record(fd)
            push(emit_event("font", **fd))
            return

        if l.startswith("free"):
            fd["license"] = "Free"
        elif "paid" in l:
            fd["license"] = "Free/Paid" if l.startswith("free") else "Paid"
        else:
            print(f"  [SUPPRESS unknown] {fd.get('name')} → {raw_lic}")
            return

        fd["is_restricted"] = False
        record(fd)
        push(emit_event("font", **fd))

    def make_fd(display: str, family: str, fkey: str,
                file_foundry, file_lic, path: str, kind: str) -> dict:
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
            font_type=classify_font_type(kind),
            is_restricted=False,
            designer=designer,
            license_detail=lic_detail,
            confidence=conf,
        )


    # ── UA profiles to rotate through on 403/block ───────────
    _UA_LIST = [
        # Chrome Windows
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br",
         "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document",
         "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"},
        # Firefox Windows
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.5", "Accept-Encoding": "gzip, deflate, br"},
        # Chrome macOS
        {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"},
        # Googlebot — enterprise/pharma sites often allow crawlers
        {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        # Curl fallback
        {"User-Agent": "curl/7.88.1", "Accept": "*/*"},
    ]

    def _fetch_page_requests(target_url):
        """Pure HTTP fetch with UA rotation."""
        import time as _t
        last_exc = None
        for i, hdrs in enumerate(_UA_LIST):
            try:
                if i > 0:
                    _t.sleep(0.5 * i)
                r = req_lib.get(target_url, headers=hdrs, timeout=25,
                                allow_redirects=True, verify=False)
                if r.status_code in (403, 401, 429) and i < len(_UA_LIST) - 1:
                    continue
                if r.status_code == 200:
                    return r.text, r.url
                r.raise_for_status()
                return r.text, r.url
            except Exception as e:
                last_exc = e
                if i < len(_UA_LIST) - 1:
                    continue
        raise last_exc or Exception(f"All fetch attempts failed for {target_url}")

    def _fetch_page_browser(target_url):
        """Playwright browser fetch — handles JS-rendered sites and WAF-blocked sites."""
        if not _PLAYWRIGHT_OK:
            raise Exception("Playwright not available")
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled",
                      "--disable-setuid-sandbox","--single-process"],
            )
            ctx = browser.new_context(
                viewport={"width":1440,"height":900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="en-US",
            )
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()

            # Intercept and collect font/CSS URLs as they load
            intercepted_urls = []
            def on_response(resp):
                url_lower = resp.url.lower()
                if any(ext in url_lower for ext in [".woff",".woff2",".ttf",".otf",".css"]):
                    intercepted_urls.append(resp.url)
            page.on("response", on_response)

            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except PWTimeout:
                pass

            # Scroll to trigger lazy-loaded fonts
            for _ in range(scroll_steps):
                page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
                page.wait_for_timeout(600)

            html     = page.content()
            final_url = page.url

            page.close(); ctx.close(); browser.close()
            return html, final_url, intercepted_urls
        finally:
            try: pw.stop()
            except: pass

    def _fetch_page(target_url):
        """Try browser first, fall back to plain HTTP."""
        try:
            push(emit_event("status", message=f"Fetching {target_url} (browser)…"))
            html, final_url, extra_urls = _fetch_page_browser(target_url)
            push(emit_event("status", message=f"  Browser fetch OK ({len(html):,} bytes)"))
            return html, final_url, extra_urls
        except Exception as e:
            push(emit_event("status", message=f"  Browser unavailable ({e}) — trying HTTP…"))
        try:
            html, final_url = _fetch_page_requests(target_url)
            push(emit_event("status", message=f"  HTTP fetch OK ({len(html):,} bytes)"))
            return html, final_url, []
        except Exception as e2:
            push(emit_event("status", message=f"  HTTP also blocked ({e2}) — scanning with empty page"))
            return "<html><head></head><body></body></html>", target_url, []

    def _parse_css_text(css_text, source_url=""):
        """Parse @font-face and font-family from a raw CSS string."""
        results = []
        seen = set()
        css_clean = _COMMENT_RE.sub("", css_text)
        for block_match in _FONT_FACE_BLOCK_RE.finditer(css_clean):
            block = block_match.group(1)
            ff_m = _FF_VALUE_RE.search(block)
            if not ff_m: continue
            raw_family = ff_m.group(1).strip().strip("'\"").strip()
            if not raw_family or not is_text_font(normalize_font(raw_family)): continue
            src_m = _SRC_URL_RE.search(block)
            src_url = src_m.group(1).strip() if src_m else ""
            if src_url and not src_url.startswith(("http://","https://","//")):
                base = source_url.rsplit("/", 1)[0]
                src_url = base + "/" + src_url.lstrip("./")
            if src_url.startswith("//"):
                src_url = "https:" + src_url
            foundry, lic = (None, None)
            if src_url.startswith("http"):
                foundry, lic = detect_from_cdn(src_url)
            nkey = normalize_font(raw_family)
            if nkey not in seen:
                seen.add(nkey)
                results.append({"display": raw_family, "family": raw_family,
                                 "foundry": foundry, "license": lic,
                                 "src_url": src_url, "kind": "font-face-css"})
        css_no_ff = _FONT_FACE_BLOCK_RE.sub("", css_clean)
        for rule_match in _RULE_BLOCK_RE.finditer(css_no_ff):
            block = rule_match.group(1)
            for ff_m in _FF_VALUE_RE.finditer(block):
                raw_val = ff_m.group(1).strip()
                for part in raw_val.split(","):
                    raw_name = part.strip().strip("'\"").strip()
                    if not raw_name: continue
                    nname = normalize_font(raw_name)
                    if not is_text_font(nname) or nname in seen: continue
                    seen.add(nname)
                    results.append({"display": raw_name, "family": raw_name,
                                    "foundry": None, "license": None,
                                    "src_url": "", "kind": "css-decl"})
        return results

    def resolve_url(href):
        if not href: return None
        href = href.strip()
        if href.startswith("http"): return href
        if href.startswith("//"): return "https:" + href
        return urljoin(base_url, href)

    # ── Fetch the page ────────────────────────────────────────
    html, base_url, browser_resource_urls = _fetch_page(url)

    try:
        soup = BeautifulSoup(html, "html.parser")

        # ── Method 1: Google Fonts <link> tags ────────────────
        push(emit_event("status", message="Method 1 — Checking Google Fonts links…"))
        gf_hrefs = []
        for tag in soup.find_all("link", href=True):
            h = tag.get("href", "")
            if "fonts.googleapis.com" in h:
                gf_hrefs.append(resolve_url(h))
        for tag in soup.find_all("style"):
            for m in re.findall(r'@import\s+url\(["\']?(https?://fonts\.googleapis\.com[^"\')\s]+)', tag.string or ""):
                gf_hrefs.append(m)
        for gf_url in set(filter(None, gf_hrefs)):
            try:
                parsed = urlparse(gf_url)
                qs = parse_qs(parsed.query)
                for fp in qs.get("family", []):
                    for fpart in fp.split("|"):
                        fname_raw = fpart.split(":")[0].replace("+", " ").strip()
                        axes_part = fpart[len(fpart.split(":")[0]):].lstrip(":")
                        if not fname_raw: continue
                        variants = []
                        if "wght@" in axes_part or "ital,wght@" in axes_part:
                            has_ital = "ital" in axes_part
                            nums = re.findall(r"\d+", axes_part)
                            if has_ital and len(nums) >= 2:
                                for ital, wght in zip(nums[::2], nums[1::2]):
                                    variants.append((ital == "1", wght))
                            else:
                                for w in nums:
                                    if int(w) >= 100:
                                        variants.append((False, w))
                        if not variants:
                            variants = [(False, "400")]
                        w_map = {"100":"Thin","200":"ExtraLight","300":"Light","400":"Regular",
                                 "500":"Medium","600":"SemiBold","700":"Bold","800":"ExtraBold","900":"Black"}
                        for is_italic, wght in variants:
                            wlabel  = w_map.get(str(wght), f"W{wght}")
                            display = fname_raw
                            if wlabel and wlabel != "Regular": display += f" {wlabel}"
                            if is_italic: display += " Italic"
                            dn   = normalize_font(display)
                            fkey = normalize_font(fname_raw)
                            if dn in seen_names: continue
                            seen_names.add(dn)
                            emb_families.add(fkey)
                            fd = make_fd(display, fname_raw, fkey, "Google", "Free (OFL)",
                                         make_css_path("fonts.googleapis.com"), "google-fonts")
                            emit_font(fd)
                            print(f"  [GF] {display}")
            except Exception as ex:
                print(f"Google Fonts parse error: {ex}")

        # ── Method 2: <link rel=preload as=font> ──────────────
        push(emit_event("status", message="Method 2 — Checking preloaded fonts…"))
        for tag in soup.find_all("link", attrs={"rel": lambda r: r and "preload" in (r if isinstance(r, list) else [r])}):
            if tag.get("as") != "font": continue
            pre_url = resolve_url(tag.get("href"))
            if not pre_url: continue
            filename  = os.path.basename(urlparse(pre_url).path)
            fkey_file = filename.lower()
            if fkey_file in seen_files or _SKIP_FILENAME_RE.search(filename):
                seen_files.add(fkey_file); continue
            seen_files.add(fkey_file)
            display_name, family_name, file_foundry, file_lic = get_font_info_from_file(pre_url)
            if not display_name: continue
            fkey = family_key(family_name or display_name)
            dn   = normalize_font(display_name)
            if not is_text_font(fkey) or dn in seen_names: continue
            seen_names.add(dn); emb_families.add(fkey)
            fd = make_fd(display_name, family_name or display_name, fkey,
                         file_foundry, file_lic, make_font_file_path(filename), "preload")
            emit_font(fd)
            print(f"  [PRELOAD] {display_name}")

        # ── Method 2b: Font files intercepted by browser ────────
        for res_url in browser_resource_urls:
            if not re.search(r"\.(woff2?|ttf|otf)(\?|#|$)", res_url, re.I):
                continue
            filename  = os.path.basename(urlparse(res_url).path)
            fkey_file = filename.lower()
            if fkey_file in seen_files or _SKIP_FILENAME_RE.search(filename):
                seen_files.add(fkey_file); continue
            seen_files.add(fkey_file)
            display_name, family_name, file_foundry, file_lic = get_font_info_from_file(res_url)
            if not display_name: continue
            fkey = family_key(family_name or display_name)
            dn   = normalize_font(display_name)
            if not is_text_font(fkey) or dn in seen_names: continue
            seen_names.add(dn); emb_families.add(fkey)
            fd = make_fd(display_name, family_name or display_name, fkey,
                         file_foundry, file_lic, make_font_file_path(filename), "embedded")
            emit_font(fd)
            print(f"  [BROWSER-FONT] {display_name}")

        # Also add CSS files intercepted by browser to our CSS parsing queue
        for res_url in browser_resource_urls:
            if not re.search(r"\.css(\?|#|$)", res_url, re.I): continue
            if "fonts.googleapis.com" in res_url: continue
            key = urlparse(res_url).path.lower()
            if key not in seen_css_set:
                seen_css_set.add(key)
                # will be picked up by Method 4 below via unique_css_urls
                # but we need to add them now before that loop
                unique_css_urls_extra = getattr(_fetch_page, '_extra_css', [])

        # ── Method 3: Collect all external CSS stylesheets ────
        push(emit_event("status", message="Method 3 — Collecting CSS stylesheets…"))
        css_urls_raw = []
        for tag in soup.find_all("link", rel=lambda r: r and "stylesheet" in (r if isinstance(r, list) else [r])):
            href = tag.get("href", "")
            if href and "fonts.googleapis.com" not in href:
                css_urls_raw.append(resolve_url(href))
        for tag in soup.find_all("style"):
            for m in re.findall(r'@import\s+url\(["\']?([^"\')\s]+)["\']?\)', tag.string or ""):
                full = resolve_url(m)
                if full and "fonts.googleapis.com" not in full:
                    css_urls_raw.append(full)
        seen_css_set = set()
        unique_css_urls = []
        for cu in filter(None, css_urls_raw):
            key = urlparse(cu).path.lower()
            if key not in seen_css_set:
                seen_css_set.add(key)
                unique_css_urls.append(cu)
        print(f"  [CSS] {len(unique_css_urls)} stylesheet(s) found")

        # ── Method 4: Parse each external CSS for @font-face ──
        push(emit_event("status", message="Method 4 — Parsing @font-face rules from CSS…"))
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
                fd = make_fd(display, family, fkey, file_foundry, file_lic,
                             make_css_path(css_url), entry["kind"])
                emit_font(fd)
        push(emit_event("status", message=f"  → {len(font_list)} fonts after external CSS"))

        # ── Method 5: Parse inline <style> blocks ─────────────
        push(emit_event("status", message="Method 5 — Parsing inline <style> blocks…"))
        for style_tag in soup.find_all("style"):
            css_text = style_tag.string or ""
            if not css_text.strip(): continue
            for entry in _parse_css_text(css_text, source_url=base_url):
                display = entry["display"].strip()
                family  = entry["family"].strip()
                fkey    = normalize_font(family)
                dn      = normalize_font(display)
                if dn in seen_names or not is_text_font(fkey): continue
                seen_names.add(dn)
                fd = make_fd(display, family, fkey, entry.get("foundry"), entry.get("license"),
                             make_css_path(base_url), entry["kind"])
                emit_font(fd)
                print(f"  [INLINE] {display}")
        push(emit_event("status", message=f"  → {len(font_list)} fonts after inline styles"))

        # ── Method 6: font-family in inline style= attributes ─
        push(emit_event("status", message="Method 6 — Scanning inline style attributes…"))
        for tag in soup.find_all(style=True):
            style_val = tag.get("style", "")
            for m in re.finditer(r"font-family\s*:\s*([^;}>]+)", style_val, re.I):
                for part in m.group(1).split(","):
                    fname = part.strip().strip("\"'")
                    if not fname or fname.lower() in ("inherit","initial","unset","revert"): continue
                    fkey = normalize_font(fname)
                    dn   = normalize_font(fname)
                    if dn in seen_names or not is_text_font(fkey): continue
                    seen_names.add(dn)
                    fd = make_fd(fname, fname, fkey, None, None,
                                 make_css_path(base_url), "inline-style")
                    emit_font(fd)
                    print(f"  [INLINE-ATTR] {fname}")

        # ── Method 7: Font CDN links (Adobe, Typekit, Bunny…) ─
        push(emit_event("status", message="Method 7 — Checking font CDN links…"))
        cdn_checks = [
            ("use.typekit.net",   "Adobe (Typekit)",   "Paid"),
            ("fonts.bunny.net",   "Bunny Fonts (CDN)", "Free (OFL)"),
            ("fast.fonts.net",    "Monotype",          "Paid"),
            ("cloud.typography",  "Hoefler & Co.",     "Paid"),
            ("kit.fontawesome",   "Font Awesome",      "Free/Paid"),
        ]
        page_text = str(soup)
        for cdn_domain, foundry, lic in cdn_checks:
            if cdn_domain not in page_text: continue
            push(emit_event("status", message=f"  Found {foundry} CDN…"))
            cdn_hrefs = []
            for tag in soup.find_all(["link","script"]):
                for attr in ("href","src"):
                    val = tag.get(attr,"")
                    if cdn_domain in val:
                        cdn_hrefs.append(resolve_url(val))
            for cdn_url in filter(None, cdn_hrefs):
                for entry in _parse_css_server_side(cdn_url):
                    display = entry["display"].strip()
                    family  = entry["family"].strip()
                    fkey    = normalize_font(family)
                    dn      = normalize_font(display)
                    if dn in seen_names or not is_text_font(fkey): continue
                    seen_names.add(dn)
                    fd = make_fd(display, family, fkey, foundry, lic,
                                 make_css_path(cdn_url), entry["kind"])
                    emit_font(fd)
                    print(f"  [CDN:{foundry}] {display}")

        # ── Done ──────────────────────────────────────────────
        total       = len(font_list)
        ai_count    = sum(1 for f in font_list if f.get("source") == "ai")
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
        queue.put_nowait(None)

# ─────────────────────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="FontScan API — Enhanced + Auth")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

@app.on_event("startup")
def _init_auth_data():
    # Ensure default admin exists even when app is started via uvicorn import path.
    init_default_admin()


# ─────────────────────────────────────────────────────────────
#  AUTH ROUTES  (public — no token needed)
# ─────────────────────────────────────────────────────────────

@app.post("/login", response_model=Token)
def login(body: UserLogin, db: Session = Depends(get_db)):
    """Email + password → JWT token (1 hour)."""
    email = body.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    return {"access_token": create_access_token({"sub": user.email}), "token_type": "bearer"}


@app.post("/signup", response_model=UserOut)
def signup(body: UserCreate, db: Session = Depends(get_db),
           authorization: str | None = Header(default=None)):
    """
    Create a new account.
    - Standard user signup is allowed.
    - Admin account creation requires an admin token.
    """
    username = body.username.strip()
    email = body.email.strip().lower()
    role = (body.role or "user").strip().lower()
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'.")
    if not username or not email:
        raise HTTPException(status_code=400, detail="Username and email are required.")

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered.")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already taken.")

    if role == "admin":
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=403, detail="Admin access required to create admin users.")
        token = authorization.split(" ", 1)[1].strip()
        current_user = verify_token_param(token)
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin access required to create admin users.")

    u = User(username=username, email=email,
             hashed_password=hash_password(body.password), role=role)
    db.add(u); db.commit(); db.refresh(u)
    return u


@app.get("/users")
def list_users(db: Session = Depends(get_db),
               current_user: User = Depends(require_admin)):
    """List all team members. Admin only."""
    return [{"id": u.id, "username": u.username, "email": u.email, "role": u.role}
            for u in db.query(User).all()]


@app.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(require_admin)):
    """Remove a team member. Admin only."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found.")
    db.delete(u); db.commit()
    return {"detail": f"User '{u.username}' deleted."}


@app.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    """Returns current logged-in user."""
    return current_user


# ─────────────────────────────────────────────────────────────
#  FRONTEND ROUTES
# ─────────────────────────────────────────────────────────────

@app.get("/")
def serve_frontend():
    return FileResponse("index.html") if os.path.exists("index.html") \
        else HTMLResponse("<h2>Place index.html next to backend.py</h2>")


@app.get("/login")
def serve_login():
    return FileResponse("login.html") if os.path.exists("login.html") \
        else HTMLResponse("<h2>Place login.html next to backend.py</h2>")


# ─────────────────────────────────────────────────────────────
#  PROTECTED API ROUTES  (require valid JWT)
# ─────────────────────────────────────────────────────────────

@app.get("/api/scan")
async def scan_endpoint(
    url:    str  = Query(...),
    wait:   int  = Query(8),
    scroll: int  = Query(3),
    use_ai: bool = Query(True),
    token:  str  = Query(...),   # JWT as query param — EventSource can't set headers
):
    verify_token_param(token)   # raises 401 if invalid/expired

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
async def estimate_traffic_endpoint(
    url:          str  = Query(...),
    current_user: User = Depends(get_current_user),
):
    if not url.startswith("http"):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
    except Exception:
        domain = url

    loop = asyncio.get_event_loop()
    raw, sw_fmt = await loop.run_in_executor(_executor, get_similarweb_traffic, domain)
    if sw_fmt:
        return {"url": url, "domain": domain, "estimate": sw_fmt, "source": "similarweb"}

    tld      = domain.split(".")[-1] if "." in domain else "com"
    estimate = await loop.run_in_executor(_executor, _estimate_traffic_gpt, url, domain, tld)
    return {"url": url, "domain": domain, "estimate": estimate,
            "source": "gpt" if estimate != "N/A" else "N/A"}


@app.get("/api/history")
def get_history(current_user: User = Depends(get_current_user)):
    items = list(reversed(list(_scan_history.values())))
    return {"history": [{k: v for k, v in h.items() if k != "fonts"} for h in items]}


@app.get("/api/history/{scan_id}")
def get_history_detail(scan_id: str, current_user: User = Depends(get_current_user)):
    h = _scan_history.get(scan_id)
    if not h:
        raise HTTPException(status_code=404, detail="Scan not found")
    return h


@app.get("/api/health")
def health(current_user: User = Depends(get_current_user)):
    ai_ok = bool(OPENAI_API_KEY) and OPENAI_API_KEY.strip() not in ("", "sk-YOUR-KEY-HERE")
    return {
        "status":             "ok",
        "user":               current_user.username,
        "cached_ai_lookups":  len(_ai_cache),
        "cached_traffic_est": 0,
        "scans_in_memory":    len(_scan_history),
        "ai_configured":      ai_ok,
        "model":              OPENAI_MODEL,
    }


# ─────────────────────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_default_admin()   # seeds admin@fontscan.com / admin123 on first run

    print("─" * 56)
    print("  FontScan Enhanced + Auth  →  http://localhost:8000")
    ai_ok = OPENAI_API_KEY and OPENAI_API_KEY.strip() not in ("", "sk-YOUR-KEY-HERE")
    print(f"  OpenAI ({OPENAI_MODEL}): {'✓ configured' if ai_ok else '✗ NOT SET'}")
    print(f"  Font AI cache:     {len(_ai_cache)} entries")
    print(f"  Auth:              JWT · SQLite · bcrypt")
    print(f"  Default admin:     admin@fontscan.com / admin123")
    print(f"  Traffic:           LIVE — SimilarWeb + GPT-4o (no cache)")
    print(f"  Chrome:            auto-detect (installed version)")
    print(f"  Features:          Traffic Est · Font Type · Restricted Section · Team Mgmt")
    print("─" * 56)
    uvicorn.run("backend:app", host="0.0.0.0", port=8000,
                reload=False, log_level="warning")