#!/usr/bin/env python3
"""
Generate `daily-news.json` (Tier 0 curated feed) for the Chinese News app.

This mirrors the on-device curation in the iOS/Android apps' NewsArticleParser:
scrape VOA 中文 (public domain) + 维基新闻 (CC BY 2.5), pull each article body,
STRIP THE FOOTER/NOISE before counting hanzi, keep 150..1500-hanzi articles, and
emit the newest ~20 as a single JSON file. Run daily from GitHub Actions and
commit the result — the apps read it first (Tier 0) and skip scraping entirely.

Stdlib only (urllib/re/json/html) so the GitHub Action needs no `pip install`.

Usage:
    python generate_daily_news.py --out daily-news.json --target 20
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import html as html_mod
import json
import re
import sys
import urllib.request

UA = ("Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
TIMEOUT = 20
MIN_HANZI = 150
MAX_HANZI = 1500

# VOA category landing pages — union of the IDs used by both apps so we gather
# the widest candidate pool. Dead IDs simply yield nothing (harmless).
VOA_SOURCES = [
    "https://www.voachinese.com/",        # homepage — broadest surface
    "https://www.voachinese.com/z/1755",  # 焦点
    "https://www.voachinese.com/z/1745",  # 印太
    "https://www.voachinese.com/z/1748",  # 经济·金融·贸易
    "https://www.voachinese.com/z/5374",  # 全球议题
    "https://www.voachinese.com/z/1759",  # 中东
    "https://www.voachinese.com/z/5679",  # 科教·文娱·体健
    "https://www.voachinese.com/z/3623",  # 中国 (legacy)
    "https://www.voachinese.com/z/1761",  # 国际 (legacy)
    "https://www.voachinese.com/z/3624",  # 美国 (legacy)
    "https://www.voachinese.com/z/1762",  # 经济 (legacy)
    "https://www.voachinese.com/z/1735",  # 时事大家谈 (legacy)
]
VOA_ALLOWED_HOSTS = {"www.voachinese.com", "voachinese.com"}
WIKINEWS_ALLOWED_HOSTS = {"zh.wikinews.org", "wikinews.org"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ----------------------------- networking -----------------------------------

def fetch(url):
    """GET `url`, decode UTF-8 then GB18030 then Latin-1. Returns str or None."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
    except Exception as e:  # noqa: BLE001 - best-effort scrape
        log(f"  fetch fail {url}: {e}")
        return None
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:  # noqa: BLE001
            continue
    return None


# ----------------------------- text utils -----------------------------------

CJK_RE = re.compile(r"[一-鿿]")


def count_cjk(s):
    return len(CJK_RE.findall(s)) if s else 0


def contains_cjk(s):
    return bool(s) and CJK_RE.search(s) is not None


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s)


def decode_entities(s):
    # html.unescape covers all named + numeric (&#NNN; / &#xHH;) entities.
    return html_mod.unescape(s) if s else s


# Footer / related-content / video-player markers — identical to the apps'
# sanitizeArticleText. Drop a line if it STARTS WITH a full-line marker
# (after the 2nd we assume the footer block began and stop), or CONTAINS a
# noise marker.
_FULL_LINE_MARKERS = ("美国之音", "VOA中文", "VOA Chinese", "相关报道", "相关链接",
                      "更多新闻", "请使用一个兼容", "评论区", "分享", "打印", "论坛", "Copyright")
_NOISE_MARKERS = ("showPlayer(", "scriptId", "videoInfo", "posterUrl",
                  "hidPlaybackRates", ".mp4", "origin.mp4")


def sanitize_article_text(text):
    if not text:
        return ""
    normalized = text.replace(" ", " ").replace("\r", "\n")
    kept = []
    footer_hits = 0
    for line in normalized.split("\n"):
        t = line.strip()
        if not t:
            continue
        if any(t.startswith(m) for m in _FULL_LINE_MARKERS):
            footer_hits += 1
            if footer_hits >= 2:
                break
            continue
        if any(m.lower() in t.lower() for m in _NOISE_MARKERS):
            continue
        kept.append(t)
    return "\n".join(kept)


_CONTAINER_PATTERNS = [
    r'<div[^>]*class\s*=\s*"[^"]*wsw[^"]*"[^>]*>',
    r'<div[^>]*class\s*=\s*"[^"]*article__body[^"]*"[^>]*>',
    r'<div[^>]*id\s*=\s*"article-content"[^>]*>',
    r'<div[^>]*class\s*=\s*"[^"]*article[^"]*"[^>]*>',
    r'<div[^>]*class\s*=\s*"[^"]*content[^"]*"[^>]*>',
    r"<article[^>]*>",
]


def extract_container_html(html):
    """Find a known article container and balance div/article tags to its end."""
    for pat in _CONTAINER_PATTERNS:
        m = re.search(pat, html, re.IGNORECASE)
        if not m:
            continue
        is_article = pat.startswith("<article")
        open_tag = "<article" if is_article else "<div"
        close_tag = "</article>" if is_article else "</div>"
        open_end = m.end()
        depth, i, n = 1, m.end(), len(html)
        low = html.lower()
        while i < n and depth > 0:
            no = low.find(open_tag, i)
            nc = low.find(close_tag, i)
            if nc == -1:
                return ""
            if no != -1 and no < nc:
                depth += 1
                i = no + len(open_tag)
            else:
                depth -= 1
                if depth == 0:
                    inner = html[open_end:nc]
                    return inner if contains_cjk(inner) else ""
                i = nc + len(close_tag)
    return ""


def extract_main_text(html):
    if not html:
        return ""
    container = extract_container_html(html)
    source = container if container else html
    lines = []
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", source, re.IGNORECASE | re.DOTALL):
        txt = decode_entities(strip_tags(m.group(1))).strip()
        if len(txt) >= 4 and contains_cjk(txt):
            lines.append(txt)
    if not lines and container:
        stripped = decode_entities(strip_tags(re.sub(r"<br", "\n<br", container, flags=re.IGNORECASE)))
        for line in stripped.split("\n"):
            t = line.strip()
            if len(t) >= 4 and contains_cjk(t):
                lines.append(t)
    if not lines:
        stripped = decode_entities(strip_tags(html))
        for line in stripped.split("\n"):
            t = line.strip()
            if len(t) >= 10 and contains_cjk(t):
                lines.append(t)
    return "\n".join(lines)


# ----------------------------- VOA scraping ---------------------------------

_ANCHOR_RE = re.compile(
    r"<a[^>]*href\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


def voa_candidates(cap):
    seen, out = set(), []
    for base in VOA_SOURCES:
        if len(out) >= cap:
            break
        html = fetch(base)
        if not html:
            continue
        added = 0
        for m in _ANCHOR_RE.finditer(html):
            if len(out) >= cap:
                break
            href = m.group(1) or m.group(2) or m.group(3)
            inner = m.group(4)
            if not href or inner is None:
                continue
            title = decode_entities(strip_tags(inner)).strip()
            if len(title) < 6 or len(title) > 120 or not contains_cjk(title):
                continue
            if "/a/" not in href or ".html" not in href.lower():
                continue
            if len(href.split("/a/", 1)[-1]) < 4:
                continue
            if href.startswith("//"):
                resolved = "https:" + href
            elif href.startswith("http://") or href.startswith("https://"):
                resolved = href.replace("http://", "https://")
            elif href.startswith("/"):
                resolved = "https://www.voachinese.com" + href
            else:
                continue
            host = re.sub(r"^https?://([^/]+).*$", r"\1", resolved).lower()
            if host not in VOA_ALLOWED_HOSTS:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append({"title": title, "link": resolved})
            added += 1
        log(f"  VOA {base} -> +{added} (total {len(out)})")
    return out


def voa_article(cand, lo, hi):
    html = fetch(cand["link"])
    if not html:
        return None
    body = sanitize_article_text(extract_main_text(html))
    hanzi = count_cjk(body)
    if hanzi < lo or hanzi > hi:
        return None
    return {
        "title": cand["title"], "link": cand["link"], "source": "voa",
        "content": body, "hanziCount": hanzi,
    }


def voa_articles(target, lo, hi):
    cands = voa_candidates(target * 6)
    out = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(voa_article, c, lo, hi) for c in cands]
        for f in cf.as_completed(futs):
            try:
                r = f.result()
            except Exception:  # noqa: BLE001
                r = None
            if r:
                out.append(r)
                if len(out) >= target:
                    break
    return out


# --------------------------- Wikinews via API -------------------------------

def wikinews_articles(target, lo, hi):
    ids = []
    cat = "Category:已发布"
    url = ("https://zh.wikinews.org/w/api.php?action=query&list=categorymembers"
           "&cmtitle=" + urllib.request.quote(cat) +
           "&cmlimit=50&cmnamespace=0&cmsort=timestamp&cmdir=desc"
           "&cmprop=ids%7Ctitle%7Ctimestamp&format=json&formatversion=2")
    raw = fetch(url)
    if raw:
        try:
            for m in json.loads(raw).get("query", {}).get("categorymembers", []):
                if m.get("pageid"):
                    ids.append(m["pageid"])
        except Exception:  # noqa: BLE001
            pass
    out = []
    for i in range(0, len(ids), 20):
        if len(out) >= target:
            break
        chunk = "|".join(str(x) for x in ids[i:i + 20])
        u = ("https://zh.wikinews.org/w/api.php?action=query&pageids=" + chunk +
             "&prop=extracts%7Cinfo&explaintext=1&exlimit=20&inprop=url"
             "&format=json&formatversion=2")
        raw = fetch(u)
        if not raw:
            continue
        try:
            pages = json.loads(raw).get("query", {}).get("pages", [])
        except Exception:  # noqa: BLE001
            continue
        for p in pages:
            title = p.get("title", "")
            extract = p.get("extract", "")
            if not title or not extract:
                continue
            body = sanitize_article_text(extract)
            hanzi = count_cjk(body)
            if hanzi < lo or hanzi > hi:
                continue
            link = p.get("fullurl") or f"https://zh.wikinews.org/?curid={p.get('pageid','')}"
            out.append({"title": title, "link": link, "source": "wikinews",
                        "content": body, "hanziCount": hanzi})
    return out


# ------------------------------- assemble -----------------------------------

def collect(target, lo, hi):
    """VOA-first, deduped by link+title, capped at target."""
    voa = voa_articles(target, lo, hi)
    wiki = wikinews_articles(max(10, target), lo, hi)
    log(f"  collected: voa={len(voa)} wikinews={len(wiki)} (filter {lo}..{hi})")
    out, seen_l, seen_t = [], set(), set()
    for src in (voa, wiki):
        for a in src:
            if len(out) >= target:
                break
            if a["link"] in seen_l or a["title"] in seen_t:
                continue
            seen_l.add(a["link"])
            seen_t.add(a["title"])
            out.append(a)
    return out


def read_previous_articles(path):
    """Load yesterday's published feed (if present) so we can backfill from it."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:  # noqa: BLE001 - missing/first run is fine
        return []
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        return doc.get("articles", []) or []
    return []


def build_feed(target, prev_articles):
    # Escalate the hanzi window if a quiet news day leaves us short of target,
    # so the published feed reliably hits `target`. Quality first, breadth only
    # when needed.
    articles = []
    for lo, hi in ((MIN_HANZI, MAX_HANZI), (120, 2200), (80, 4000)):
        articles = collect(target, lo, hi)
        log(f"pass {lo}..{hi}: {len(articles)} articles")
        if len(articles) >= target:
            break

    # GUARANTEE 20 QUALITY: if today's live scrape still fell short (VOA/维基
    # outage, very quiet day), top up with the freshest articles from the
    # PREVIOUS feed — still real, recently-curated news, never random filler.
    # The app dedups by link, so carried-over items simply stay in 지난 기사.
    if len(articles) < target and prev_articles:
        seen = {a.get("link") for a in articles} | {a.get("title") for a in articles}
        for a in prev_articles:
            if len(articles) >= target:
                break
            if a.get("link") in seen or a.get("title") in seen:
                continue
            seen.add(a.get("link"))
            seen.add(a.get("title"))
            articles.append(a)
        log(f"after backfill from previous feed: {len(articles)} articles")

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    # Only (re)stamp items that don't already carry a date, so carried-over
    # articles keep their real original publishedDate.
    for a in articles:
        a.setdefault("publishedDate", today)
    return {
        "schemaVersion": 1,
        "date": today,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "count": len(articles[:target]),
        "articles": articles[:target],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="daily-news.json")
    ap.add_argument("--target", type=int, default=20)
    args = ap.parse_args()

    prev = read_previous_articles(args.out)
    feed = build_feed(args.target, prev)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    log(f"WROTE {args.out}: {feed['count']} articles for {feed['date']}")
    # Hard-fail CI only if we have NOTHING at all (no live scrape AND no
    # previous feed to fall back on) — that's a real outage worth surfacing.
    return 0 if feed["count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
