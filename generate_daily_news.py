#!/usr/bin/env python3
"""
Generate `daily-news.json` (Tier 0 curated feed) for the Chinese News app.

This mirrors the on-device curation in the iOS/Android apps' NewsArticleParser:
scrape VOA 中文 (public domain) + 联合国新闻 (UN News, reproducible) + 维基新闻
(CC BY 2.5), pull each article body, STRIP THE FOOTER/NOISE, keep 150..1500-hanzi
articles. The feed is a NORMAL news feed (politics included); the only content
block is 台独 / 法轮功 (is_allowed). Today's fresh articles sit on top of a ROLLING
window (carried over from the previous feed, deduped, capped at --cap) so a fresh
install sees a full ~50-article list. All sources are copyright-safe (public-domain
/ reproducible / CC) and stored as inline full text. Run daily from GitHub Actions
and commit to the `feed` branch — apps read it first (Tier 0).

Stdlib only (urllib/re/json/html/gzip) so the GitHub Action needs no `pip install`.

Usage:
    python generate_daily_news.py --out daily-news.json --target 30 --cap 50
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import html as html_mod
import json
import re
import sys
import urllib.request
from itertools import zip_longest

UA = ("Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
TIMEOUT = 20
MIN_HANZI = 150
MAX_HANZI = 1500

# VOA category landing pages — the FULL set (general news, NOT restricted to
# neutral sections). PRODUCT DECISION: the realtime feed is a normal news feed
# (politics included); only 台独 / 法轮功 are blocked downstream (see is_allowed).
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
]

# Every published article body is truncated to this many CJK chars (at a line
# boundary) — a sensible reading-session length, independent of the acceptance
# window used to QUALIFY the article.
DISPLAY_MAX_HANZI = 1200

# Block list — PRODUCT DECISION: the feed is otherwise UNFILTERED (normal news,
# politics included). We block ONLY articles touching 台独 (Taiwan independence) or
# 法轮功 (Falun Gong), thoroughly, over the FULL body. A regex with [简/繁] character
# classes covers simplified / traditional / mixed forms (台/臺, 独/獨, 湾/灣, 轮/輪).
_BLOCK_RE = re.compile(
    r"[台臺][独獨]"            # 台独 / 台獨 / 臺独 / 臺獨
    r"|[台臺][湾灣][独獨]立"    # 台湾独立 full phrase (all simp/trad mixes)
    r"|法[轮輪]"               # 法轮功 / 法轮大法 / 法轮 (FG org + practice)
)


def is_allowed(title, body=""):
    """False only when the item touches 台独 / 法轮功 (checked over the FULL text)."""
    return _BLOCK_RE.search((title or "") + " " + (body or "")) is None


# Headline-roundup / broadcast digests — lists of mixed headlines, not a readable
# article. Dropped for READING QUALITY only (independent of the block list above).
_DIGEST_MARKERS = ("报纸头条", "報紙頭條", "中文报纸头条", "子午播报", "播报",
                   "中文广播", "美国之音中文广播", "新闻简报")


def is_digest(title):
    return any(m in (title or "") for m in _DIGEST_MARKERS)


def truncate_hanzi(text, max_hanzi):
    """Trim body to ~max_hanzi CJK chars at a line boundary (keep whole lines)."""
    if not text or count_cjk(text) <= max_hanzi:
        return text
    kept, total = [], 0
    for line in text.split("\n"):
        kept.append(line)
        total += count_cjk(line)
        if total >= max_hanzi:
            break
    return "\n".join(kept)
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
        "Accept-Encoding": "identity",   # some CDNs (UN News) gzip otherwise
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip" or data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except Exception:  # noqa: BLE001
                    pass
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
    r'<div[^>]*class\s*=\s*"[^"]*field--name-body[^"]*"[^>]*>',   # UN News (Drupal)
    r'<div[^>]*class\s*=\s*"[^"]*text-formatted[^"]*"[^>]*>',     # UN News (Drupal)
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
    if is_digest(cand["title"]) or not is_allowed(cand["title"], body):
        return None
    body = truncate_hanzi(body, DISPLAY_MAX_HANZI)
    return {
        "title": cand["title"], "link": cand["link"], "source": "voa",
        "content": body, "hanziCount": count_cjk(body),
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
            if is_digest(title) or not is_allowed(title, body):
                continue
            body = truncate_hanzi(body, DISPLAY_MAX_HANZI)
            link = p.get("fullurl") or f"https://zh.wikinews.org/?curid={p.get('pageid','')}"
            out.append({"title": title, "link": link, "source": "wikinews",
                        "content": body, "hanziCount": count_cjk(body)})
    return out


# ----------------------- UN News (RSS + topic pages) ------------------------
# 联合国新闻 (news.un.org) — UN content, freely reproducible with attribution,
# covering world / economy / health / climate / culture / politics. Full text
# stored inline (same as VOA/维基), so the apps render it without on-device scraping.
# We pull the all-news RSS PLUS the topic landing pages to widen the pool. All
# topics are included (the feed is a normal news feed; only 台独 / 法轮功 are blocked).
UNNEWS_RSS = "https://news.un.org/feed/subscribe/zh/news/all/rss.xml"
UNNEWS_TOPICS = ["economic-development", "climate-change", "culture-and-education",
                 "health", "sdgs", "peace-and-security", "human-rights",
                 "humanitarian-aid", "migrants-and-refugees",
                 "law-and-crime-prevention", "un-affairs", "women"]
UNNEWS_ALLOWED_HOSTS = {"news.un.org"}

_RSS_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)
_RSS_LINK_RE = re.compile(r"<link>(.*?)</link>", re.IGNORECASE | re.DOTALL)
_RSS_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_UN_STORY_RE = re.compile(r"/zh/story/\d{4}/\d{2}/\d+")
_OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)


def _clean_cdata(s):
    return re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.DOTALL) if s else s


def rss_items(xml):
    """Parse <item> title+link from an RSS feed → [{title, link}]."""
    items = []
    for m in _RSS_ITEM_RE.finditer(xml or ""):
        block = m.group(1)
        lm = _RSS_LINK_RE.search(block)
        tm = _RSS_TITLE_RE.search(block)
        if not lm or not tm:
            continue
        link = decode_entities(strip_tags(_clean_cdata(lm.group(1)))).strip()
        title = decode_entities(strip_tags(_clean_cdata(tm.group(1)))).strip()
        if link and title and contains_cjk(title):
            items.append({"title": title, "link": link})
    return items


def _unnews_title(html_text):
    """Resolve a UN story title from og:title (clean) → h1, for topic-page links."""
    m = _OG_TITLE_RE.search(html_text or "")
    if m:
        return decode_entities(strip_tags(m.group(1))).strip()
    m = _H1_RE.search(html_text or "")
    if m:
        return re.sub(r"\s+", " ", decode_entities(strip_tags(m.group(1)))).strip()
    return ""


def unnews_candidates(cap):
    """UN story candidates: the all-news RSS (titled) + neutral topic pages
    (links only; titles resolved per-article). Deduped by canonical /zh/story/ link."""
    seen, out = set(), []
    for it in rss_items(fetch(UNNEWS_RSS)):
        link = it["link"].replace("/feed/view/zh/", "/zh/")
        if link in seen:
            continue
        seen.add(link)
        out.append({"title": it["title"], "link": link})
        if len(out) >= cap:
            return out
    for topic in UNNEWS_TOPICS:
        if len(out) >= cap:
            break
        page = fetch(f"https://news.un.org/zh/news/topic/{topic}")
        if not page:
            continue
        for path in dict.fromkeys(_UN_STORY_RE.findall(page)):   # ordered dedup
            link = "https://news.un.org" + path
            if link in seen:
                continue
            seen.add(link)
            out.append({"title": "", "link": link})
            if len(out) >= cap:
                break
    return out


def unnews_article(cand, lo, hi):
    html = fetch(cand["link"])
    if not html:
        return None
    title = cand["title"] or _unnews_title(html)
    if not title or not contains_cjk(title):
        return None
    body = sanitize_article_text(extract_main_text(html))
    hanzi = count_cjk(body)
    if hanzi < lo or hanzi > hi:
        return None
    if is_digest(title) or not is_allowed(title, body):
        return None
    body = truncate_hanzi(body, DISPLAY_MAX_HANZI)
    # Normalize the feed/view redirect to the canonical story URL for "view original".
    link = cand["link"].replace("/feed/view/zh/", "/zh/")
    return {"title": title, "link": link, "source": "unnews",
            "content": body, "hanziCount": count_cjk(body)}


def unnews_articles(target, lo, hi):
    cands = unnews_candidates(max(target, 15) * 3)
    out = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(unnews_article, c, lo, hi) for c in cands]
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


# ------------------------------- assemble -----------------------------------

def collect(target, lo, hi):
    """VOA + UN News + Wikinews, INTERLEAVED for variety (so VOA doesn't crowd out
    the others), deduped by link+title, capped at target."""
    voa = voa_articles(target, lo, hi)
    un = unnews_articles(max(15, target), lo, hi)
    wiki = wikinews_articles(max(10, target), lo, hi)
    log(f"  collected: voa={len(voa)} unnews={len(un)} wikinews={len(wiki)} (filter {lo}..{hi})")
    out, seen_l, seen_t = [], set(), set()
    # Round-robin: voa[0], un[0], wiki[0], voa[1], un[1], ... keeps the top of the
    # feed diverse instead of a solid block of VOA.
    for a in (x for grp in zip_longest(voa, un, wiki) for x in grp if x):
        if len(out) >= target:
            break
        link, title = a.get("link"), a.get("title")
        if not link or not title:
            continue
        if link in seen_l or title in seen_t:
            continue
        seen_l.add(link)
        seen_t.add(title)
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


def build_feed(fresh_target, rolling_cap, prev_articles):
    # Scrape today's FRESH articles, escalating the hanzi window on a quiet news
    # day so we still gather a healthy batch. Quality first, breadth only when
    # needed.
    fresh = []
    for lo, hi in ((MIN_HANZI, MAX_HANZI), (120, 2200), (80, 4000)):
        fresh = collect(fresh_target, lo, hi)
        log(f"pass {lo}..{hi}: {len(fresh)} fresh articles")
        if len(fresh) >= fresh_target:
            break

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    # Only stamp the fresh items; carried-over articles keep their real publishedDate.
    for a in fresh:
        a.setdefault("publishedDate", today)

    # Carryover from the PREVIOUS feed must pass the SAME digest + block-list gates
    # the fresh batch did. Older curator versions (or expanded marker lists) may have
    # published items that shouldn't ride the rolling window forever, so re-check them
    # here instead of trusting that whatever was published before is still acceptable.
    carry = [a for a in (prev_articles or [])
             if not is_digest(a.get("title", ""))
             and is_allowed(a.get("title", ""), a.get("content", ""))]

    # ROLLING WINDOW: today's fresh articles on top, then carry over the most-recent
    # (re-filtered) items from the previous feed (deduped by link+title), capped at
    # rolling_cap. The oldest fall off the tail. This keeps the published feed at
    # ~rolling_cap so a fresh install shows a full list immediately, while genuinely-
    # new articles always sit at the top. The window fills to the cap over a few runs.
    out, seen_l, seen_t = [], set(), set()
    for a in fresh + carry:
        if len(out) >= rolling_cap:
            break
        link, title = a.get("link"), a.get("title")
        if not link or not title:
            continue
        if link in seen_l or title in seen_t:
            continue
        seen_l.add(link)
        seen_t.add(title)
        out.append(a)
    log(f"rolling window: {len(fresh)} fresh + carryover -> {len(out)} (cap {rolling_cap})")

    return {
        "schemaVersion": 1,
        "date": today,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "count": len(out),
        "articles": out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="daily-news.json")
    ap.add_argument("--target", type=int, default=30,
                    help="how many FRESH articles to scrape each day")
    ap.add_argument("--cap", type=int, default=50,
                    help="published rolling-window size (fresh + carryover)")
    args = ap.parse_args()

    prev = read_previous_articles(args.out)
    feed = build_feed(args.target, args.cap, prev)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    log(f"WROTE {args.out}: {feed['count']} articles for {feed['date']}")
    # Hard-fail CI only if we have NOTHING at all (no live scrape AND no
    # previous feed to fall back on) — that's a real outage worth surfacing.
    return 0 if feed["count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
