"""Product photos from one vendor product page (or datasheet address) the operator names.

Everything here is user-initiated for ONE address: LinkedEye reads that page once, picks the picture addresses out of its
markup and downloads those pictures, nothing else. It never follows a link, crawls a site or tries to get past bot
protection: it says plainly who it is (a `LinkedEye/<version>` User-Agent, never a browser's), and when a site answers
403 / 429 or shows a challenge page it stops and tells the operator to download the datasheet PDF in their own browser and
upload it. Every outbound byte goes through probes.checks.fetch_bytes (https only, public addresses only, redirects
re-validated, size and time limited).

parse_page() is a pure function over HTML text (stdlib html.parser only); fetch_document() does the one page fetch and
routes by what came back (HTML, PDF or an image); download_images() fetches the picked picture addresses in parallel."""
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from django.conf import settings

from probes import checks

from .imaging import MAX_BYTES as MAX_IMAGE_BYTES
from .imaging import ImageError, inspect_image, sniff

MAX_HTML_BYTES = 6 * 1024 * 1024    # vendor product-guide pages inline whole guides (a 3.6 MB page is normal)
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_CANDIDATES = 12
PARALLEL = 4
PAGE_TIMEOUT = 30                  # seconds for the one page or PDF (redirects included); a gunicorn request has 60
IMAGE_TIMEOUT = 10                 # per picture
DOWNLOAD_BUDGET = 20               # all pictures together
PARSE_BUDGET = 4.0                 # seconds spent parsing markup
MAX_TAGS = 60000
MIN_WIDTH, MIN_HEIGHT = 200, 120
MAX_ASPECT = 5.0

HTML_TYPES = {"text/html", "application/xhtml+xml"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/pjpeg", "image/x-png", "image/webp"}
GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream", ""}
DOC_LIMITS = {**{t: MAX_HTML_BYTES for t in HTML_TYPES}, "application/pdf": MAX_PDF_BYTES, "application/x-pdf": MAX_PDF_BYTES,
              **{t: MAX_IMAGE_BYTES for t in IMAGE_TYPES}, **{t: MAX_PDF_BYTES for t in GENERIC_TYPES}}
ACCEPT_DOC = "text/html,application/xhtml+xml,application/pdf;q=0.9,image/png,image/jpeg,image/webp;q=0.8"
ACCEPT_IMAGE = "image/png,image/jpeg,image/webp;q=0.9,*/*;q=0.1"

BLOCKED_MESSAGE = ("This site blocks automated downloads. Download the datasheet PDF yourself and upload it here.")


def user_agent():
    """Who we are, honestly: the product and its version, nothing that imitates a browser."""
    return f"LinkedEye/{settings.LINKEDEYE_VERSION} (product photo fetch)"


class DatasheetError(Exception):
    """A failure with a sentence that is safe and useful to show the operator. `blocked`: the site refuses automated
    downloads, so the view offers the datasheet upload."""

    def __init__(self, message, blocked=False):
        super().__init__(message)
        self.blocked = blocked


# ------------------------------------------------------------------------------------------ error wording
def explain(res, what="page"):
    """The operator-facing sentence for a failed probes.checks.FetchResult."""
    status, err = res.status, (res.error or "")
    if status in (401, 403, 429, 451):
        return DatasheetError(BLOCKED_MESSAGE, blocked=True)
    if status in (404, 410):
        return DatasheetError(f"That address was not found (HTTP {status}). Check the link, or search for the datasheet and copy its address.")
    if err.startswith("Timed out"):
        return DatasheetError("The site did not answer in time. Try again, or download the datasheet PDF and upload it here.")
    if "larger than" in err:
        limit = {"page": f"{MAX_HTML_BYTES // 1048576} MB", "image": "2 MB", "pdf": "25 MB"}.get(what, "")
        return DatasheetError(f"That file is bigger than the {limit} LinkedEye reads from one address. "
                              "Download it yourself and upload it here instead.")
    if err.startswith("the server sent"):
        kind = err.split("sent", 1)[1].split(",", 1)[0].strip()
        return DatasheetError(f"That address is not a web page, a PDF or a PNG, JPEG or WebP picture (the server sent {kind}).")
    if err.startswith("Blocked by probe policy"):
        return DatasheetError(err)
    if err.startswith("TLS"):
        return DatasheetError(f"The site's secure connection could not be verified ({err}). Nothing was downloaded.")
    if "redirect" in err:
        # Sites that send automated clients round in circles are refusing them; say so and offer the upload.
        return DatasheetError("The site kept redirecting this address instead of showing the page. Download the datasheet PDF "
                              "yourself and upload it here.", blocked=True)
    if status and status >= 500:
        return DatasheetError(f"The site had a problem answering (HTTP {status}). Try again later; if it keeps happening the site "
                              "may be refusing automated downloads, so download the datasheet PDF yourself and upload it here.")
    if status and status != 200:
        return DatasheetError(f"The site answered HTTP {status}, so nothing was downloaded.")
    return DatasheetError(f"Could not fetch that address: {err or 'no answer'}")


_CHALLENGE = re.compile(
    r"just a moment|checking your browser|cf-browser-verification|cf-challenge|challenge-platform|captcha|access denied|"
    r"are you a robot|verify you are human|verify that you are human|enable javascript|_incapsula_|pardon our interruption|"
    r"unusual traffic|request unsuccessful|ddos protection|bot detection|perimeterx|px-captcha", re.I)


def looks_blocked(html):
    """A challenge / captcha / JavaScript wall instead of the product page."""
    return bool(_CHALLENGE.search(html[:300_000]))


# ------------------------------------------------------------------------------------------ fetching
@dataclass
class Document:
    kind: str                    # "html" | "pdf" | "image"
    body: bytes
    url: str                     # the final address, after redirects
    content_type: str = ""


def check_url(url):
    """The address the operator typed, or DatasheetError. Syntax only (https, a host, a valid port, no user name or
    password, no control characters); where it points is decided by probes.checks when it is fetched."""
    url = (url or "").strip()
    if not url:
        raise DatasheetError("Enter the address of a product page, a datasheet or a picture, or choose a PDF file.")
    if len(url) > 500:
        raise DatasheetError("That address is too long (500 characters at most).")
    invalid = DatasheetError("That is not a valid https:// address.")
    try:
        parts = urlsplit(url)
        host, _port = parts.hostname, parts.port           # either raises ValueError for [::1, :abc or :99999
    except ValueError:
        raise invalid from None
    if parts.scheme.lower() != "https":
        raise DatasheetError("Only https:// addresses can be fetched. Paste the address from your browser's address bar.")
    if not host or any(ord(c) < 33 or ord(c) == 127 or c in "<>\"'\\" for c in url):
        raise invalid
    if parts.username is not None or parts.password is not None:
        raise DatasheetError("Addresses that carry a user name or password are not fetched. Remove that part of the address.")
    return url


def fetch_document(url):
    """One GET of `url`; returns a Document routed by what came back. Raises DatasheetError."""
    url = check_url(url)
    res = checks.fetch_bytes(url, MAX_HTML_BYTES, timeout=PAGE_TIMEOUT, accept=ACCEPT_DOC, https_only=True,
                             user_agent=user_agent(), type_limits=DOC_LIMITS)
    if not res.ok:
        raise explain(res, "page")
    body, ctype, final = res.body, (res.content_type or ""), (res.final_url or url)
    head = body[:1024]
    if sniff(body) is not None:
        return Document("image", body, final, ctype)
    if b"%PDF-" in head:
        return Document("pdf", body, final, ctype)
    if ctype in HTML_TYPES or (ctype in GENERIC_TYPES and head.lstrip()[:1] == b"<"):
        if len(body) > MAX_HTML_BYTES:
            raise DatasheetError(f"That page is bigger than the {MAX_HTML_BYTES // 1048576} MB LinkedEye reads from one address. "
                                 "Download the datasheet PDF yourself and upload it here instead.")
        return Document("html", body, final, ctype)
    raise DatasheetError("That address is not a web page, a PDF or a PNG, JPEG or WebP picture.")


# ------------------------------------------------------------------------------------------ parsing
RANK = {"og:image": 1, "og:image:secure_url": 2, "twitter:image": 3, "image_src": 4, "json-ld": 5, "img": 6}
LABELS = {"og:image": "og:image", "og:image:secure_url": "og:image", "twitter:image": "twitter:image",
          "image_src": "page link (image_src)", "json-ld": "product data (JSON-LD)", "img": "page image"}
META_KEYS = {"og:image": "og:image", "og:image:url": "og:image", "og:image:secure_url": "og:image:secure_url",
             "twitter:image": "twitter:image", "twitter:image:src": "twitter:image"}
_BAD_EXT = (".svg", ".svgz", ".gif", ".ico", ".avif", ".bmp", ".tif", ".tiff", ".heic")
_JUNK_WORDS = re.compile(r"(?<![a-z])(logos?|icons?|banners?|avatars?|flags?|sprites?|favicons?|spacers?|pixels?|placeholders?|"
                         r"spinners?|loaders?|badges?|buttons?|arrows?|social|tracking|beacon|gartner|awards?|webinars?|ebooks?|whitepapers?|"
                         r"testimonials?|headshots?|keynotes?)(?![a-z])", re.I)
_JUNK_DIRS = re.compile(r"/(logos?|icons?|banners?|avatars?|flags?|sprites?|favicons?|news|awards?|diagrams?|people|team|events?|"
                        r"webinars?|testimonials?|partners?|promos?|videos?|press|ebooks?|checklists?|whitepapers?)/|\.pdf\.", re.I)   # ".pdf." = a rendered cover of some PDF
# What a picture shows, from its file name, alt text, title and caption: a straight or angled view of the front is the one that
# identifies the device; internals, rear panels, diagrams and callout drawings are not.
_VIEW_GOOD = re.compile(r"(?<![a-z])(front|forward|hero|main|angled?|angle|primary)(?![a-z])", re.I)
_VIEW_BAD = re.compile(r"(?<![a-z])(rear|back|internal|inside|interior|diagrams?|callouts?|blocks?|architecture|topology|schematic|"
                       r"dimensions?|drawing|top[ _-]?view|configurations?|bays|chart|table|comparison|compare)(?![a-z])", re.I)
WIDE = 2.6                                   # wider than this (width / height): a banner-like crop; ranked after squarer shots
_TRACK_URL = re.compile(r"doubleclick|googleads|google-analytics|facebook\.com/tr|/pixel|/beacon|1x1|/track|spacer\.", re.I)
_PAGE_LEVEL = ("og:image", "og:image:secure_url", "twitter:image")
_SRCSET_DESC = re.compile(r"^(\d+(?:\.\d+)?)([wx]),?$")


@dataclass
class PageImage:
    url: str
    source: str                   # key of RANK
    alt: str = ""
    caption: str = ""
    order: int = 0
    bonus: int = 0                # 2 / 1: the whole model / its model number appears in the file name, alt text or caption
    boost: int = 0                # +1 for a page-level picture (og:image, twitter:image) that also names the model
    view: int = 0                 # +1 front / hero view, -1 rear, internal, diagram or callout drawing

    @property
    def score(self):
        return self.bonus + self.boost

    @property
    def label(self):
        return LABELS.get(self.source, "page image")


def _clean_url(raw, base):
    raw = (raw or "").strip()
    if not raw or len(raw) > 1500 or raw[:5].lower() in ("data:", "javas", "blob:", "about"):
        return None
    if any(ord(c) < 33 or ord(c) == 127 for c in raw):          # control characters or spaces: not an address we would send
        return None
    try:
        url = urljoin(base, raw)
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return None
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def _srcset(value):
    """[(url, weight)] from a srcset attribute, largest first; weight is the w or x descriptor (1 when absent)."""
    out, last = [], None
    for tok in (value or "").split():
        m = _SRCSET_DESC.match(tok)
        if m and last is not None:
            out[last] = (out[last][0], float(m.group(1)) * (1 if m.group(2) == "w" else 1000))
            last = None
            continue
        url = tok.rstrip(",")
        if url:
            out.append((url, 1.0))
            last = len(out) - 1 if not tok.endswith(",") else None
    return sorted(out, key=lambda t: -t[1])


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _model_bonus(model, *texts):
    """2 when the whole model string appears in the alt / file name / caption, 1 when its model number does."""
    full = _norm(model)
    if len(full) < 3:
        return 0
    hay = _norm(" ".join(t for t in texts if t))
    if full in hay:
        return 2
    for tok in re.split(r"[\s/_\-.,]+", model or ""):
        n = _norm(tok)
        if len(n) >= 3 and any(c.isdigit() for c in n) and n in hay:
            return 1
    return 0


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []             # dicts: raw, source, alt, title, cls, w, h, caption, order
        self.ld = []                # JSON-LD script texts
        self._ld_buf = None
        self._ld_len = 0
        self._tags = 0
        self._pic_sources = []
        self._in_picture = False
        self._fig_depth = 0
        self._fig_items = []
        self._cap_buf = None
        self._fig_caption = ""
        self.truncated = False

    # --- helpers
    def _add(self, raw, source, **kw):
        if raw:
            item = {"raw": raw, "source": source, "alt": "", "title": "", "cls": "", "w": None, "h": None, "caption": "",
                    "order": len(self.items)}
            item.update(kw)
            self.items.append(item)
            return item
        return None

    @staticmethod
    def _px(value):
        m = re.match(r"\s*(\d+)", value or "")
        return int(m.group(1)) if m else None

    # --- parser callbacks
    def handle_starttag(self, tag, attrs):
        self._tags += 1
        if self._tags > MAX_TAGS:
            self.truncated = True
            return
        a = {k.lower(): (v or "") for k, v in attrs if k}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
            if key in META_KEYS:
                self._add(a.get("content"), META_KEYS[key])
        elif tag == "link":
            if "image_src" in a.get("rel", "").lower().split():
                self._add(a.get("href"), "image_src")
        elif tag == "script":
            if "ld+json" in a.get("type", "").lower():
                self._ld_buf, self._ld_len = [], 0
        elif tag == "picture":
            self._in_picture, self._pic_sources = True, []
        elif tag == "figure":
            self._fig_depth += 1
            if self._fig_depth == 1:
                self._fig_items, self._fig_caption = [], ""
        elif tag == "figcaption":
            self._cap_buf = []
        elif tag in ("img", "source"):
            if tag == "source" and not self._in_picture:
                return
            cls = " ".join([a.get("class", ""), a.get("id", "")])
            base = dict(alt=a.get("alt", ""), title=a.get("title", ""), cls=cls, w=self._px(a.get("width")), h=self._px(a.get("height")))
            made = []
            sets = [a.get("srcset"), a.get("data-srcset")]
            for s in sets:
                for n, (u, _w) in enumerate(_srcset(s)[:2]):
                    made.append(self._add(u, "img", **base))
            if tag == "img":
                for key in ("src", "data-src", "data-lazy-src", "data-original", "data-lazy"):
                    if a.get(key) and not a[key].startswith("data:"):
                        made.append(self._add(a[key], "img", **base))
            elif a.get("src"):
                made.append(self._add(a["src"], "img", **base))
            made = [m for m in made if m]
            if tag == "source":
                self._pic_sources.extend(made)
            else:
                for m in self._pic_sources:           # the <img> of a <picture> carries the alt for its <source>s
                    m["alt"] = m["alt"] or base["alt"]
                self._pic_sources = []
            if self._fig_depth:
                for m in made:                         # a caption that came before the picture still belongs to it
                    m["caption"] = m["caption"] or self._fig_caption
                self._fig_items.extend(made)

    def handle_endtag(self, tag):
        if tag == "script" and self._ld_buf is not None:
            self.ld.append("".join(self._ld_buf)[:300_000])
            self._ld_buf = None
        elif tag == "picture":
            self._in_picture, self._pic_sources = False, []
        elif tag == "figcaption" and self._cap_buf is not None:
            text = " ".join("".join(self._cap_buf).split())[:300]
            self._cap_buf = None
            self._fig_caption = text
            for it in self._fig_items:
                it["caption"] = it["caption"] or text
        elif tag == "figure" and self._fig_depth:
            self._fig_depth -= 1

    def handle_data(self, data):
        if self._ld_buf is not None and self._ld_len < 300_000:       # a running total: summing the list per call is quadratic
            self._ld_buf.append(data)
            self._ld_len += len(data)
        if self._cap_buf is not None and len(self._cap_buf) < 200:
            self._cap_buf.append(data)

    def error(self, message):       # pragma: no cover - HTMLParser compatibility
        pass


def _ld_images(texts):
    """Image addresses of schema.org Product entries in JSON-LD blocks."""
    out, nodes = [], 0

    def pick(v, depth=0):
        if depth > 3:
            return
        if isinstance(v, str):
            out.append(v.replace("&amp;", "&"))        # markup escaping inside a script block is not undone by the parser
        elif isinstance(v, list):
            for x in v[:10]:
                pick(x, depth + 1)
        elif isinstance(v, dict):
            pick(v.get("url") or v.get("contentUrl"), depth + 1)

    def walk(node, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 6 or nodes > 800:
            return
        if isinstance(node, list):
            for x in node[:200]:
                walk(x, depth + 1)
        elif isinstance(node, dict):
            t = node.get("@type")
            types = [t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)]
            if any(x.lower() in ("product", "productmodel", "individualproduct") for x in types):
                pick(node.get("image"))
            for k in ("@graph", "mainEntity", "itemListElement", "item", "hasVariant", "isVariantOf"):
                if k in node:
                    walk(node[k], depth + 1)

    for text in texts:
        try:
            walk(json.loads(text))
        except (ValueError, RecursionError):
            continue
    return out


def parse_page(html, base_url, model=""):
    """Ranked PageImage candidates (at most MAX_CANDIDATES) from the page markup. Pure: no network, no I/O."""
    parser = _Collector()
    stop = time.monotonic() + PARSE_BUDGET
    try:
        for i in range(0, min(len(html), MAX_HTML_BYTES), 65536):
            parser.feed(html[i:i + 65536])
            if parser.truncated or time.monotonic() > stop:
                break
        parser.close()
    except Exception:  # noqa: BLE001 - hostile markup: use what was collected
        pass
    items = list(parser.items)
    for raw in _ld_images(parser.ld):
        items.append({"raw": raw, "source": "json-ld", "alt": "", "title": "", "cls": "", "w": None, "h": None, "caption": "",
                      "order": len(items)})
    best = {}
    for it in items:
        url = _clean_url(it["raw"], base_url)
        if not url:
            continue
        path = urlsplit(url).path
        name = path.rsplit("/", 1)[-1]
        if path.lower().endswith(_BAD_EXT) or _TRACK_URL.search(url) or _JUNK_DIRS.search(path):
            continue
        w, h = it["w"], it["h"]
        if (w is not None and w <= 3) or (h is not None and h <= 3) or (w is not None and h is not None and w <= 64 and h <= 64):
            continue
        if _JUNK_WORDS.search(" ".join([name, it["alt"], it["title"], it["cls"]])):
            continue
        about = " ".join([name, it["alt"], it["title"], it["caption"]])
        bonus = _model_bonus(model, name, it["alt"], it["title"], it["caption"])
        cand = PageImage(url, it["source"], alt=it["alt"] or it["title"], caption=it["caption"], order=it["order"], bonus=bonus,
                         boost=1 if bonus and it["source"] in _PAGE_LEVEL else 0,
                         view=bool(_VIEW_GOOD.search(about)) - bool(_VIEW_BAD.search(about)))
        old = best.get(url)
        if old is None or (RANK[cand.source], cand.order) < (RANK[old.source], old.order):
            best[url] = cand
    ranked = sorted(best.values(), key=lambda c: (-c.score, RANK[c.source], -c.view, c.order))
    return ranked[:MAX_CANDIDATES]


# ------------------------------------------------------------------------------------------ downloading the pictures
@dataclass
class Downloaded:
    page_image: PageImage
    data: bytes
    content_type: str
    width: int
    height: int


@dataclass
class DownloadStats:
    tried: int = 0
    refused: int = 0              # the picture's server answered 403 / 429 / 401
    failed: int = 0
    unusable: int = 0             # downloaded but not an acceptable photo, or too small / banner shaped
    late: int = 0                 # not finished inside the overall time budget
    duplicates: int = 0


def _fetch_one(url, timeout):
    return checks.fetch_bytes(url, MAX_IMAGE_BYTES, timeout=timeout, accept=ACCEPT_IMAGE, https_only=True,
                              accepted_types=IMAGE_TYPES | GENERIC_TYPES, user_agent=user_agent())


def download_images(images, *, budget=DOWNLOAD_BUDGET, workers=PARALLEL):
    """Fetch each PageImage (in parallel, at most `workers` at a time, inside `budget` seconds) and validate the bytes.
    Returns (list[Downloaded] in the order of `images`, DownloadStats)."""
    stats = DownloadStats(tried=len(images))
    if not images:
        return [], stats
    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(images))))
    futures = {}
    for img in images:
        futures[pool.submit(lambda u=img.url: _fetch_one(u, max(1.0, min(IMAGE_TIMEOUT, budget - (time.monotonic() - started)))))] = img
    done, _pending = wait(futures, timeout=budget)
    pool.shutdown(wait=False, cancel_futures=True)
    out, seen = [], set()
    for fut, img in futures.items():       # dict preserves submission order = rank order
        if fut not in done:
            stats.late += 1
            continue
        try:
            res = fut.result()
        except Exception:  # noqa: BLE001
            stats.failed += 1
            continue
        if not res.ok:
            if res.status in (401, 403, 429):
                stats.refused += 1
            else:
                stats.failed += 1
            continue
        try:
            info = inspect_image(res.body, declared_type=res.content_type or None)
        except ImageError:
            stats.unusable += 1
            continue
        if info.width < MIN_WIDTH or info.height < MIN_HEIGHT or max(info.width / info.height, info.height / info.width) > MAX_ASPECT:
            stats.unusable += 1
            continue
        key = hashlib.sha256(res.body).digest()
        if key in seen:
            stats.duplicates += 1
            continue
        seen.add(key)
        out.append(Downloaded(img, res.body, info.content_type, info.width, info.height))
    return out, stats
