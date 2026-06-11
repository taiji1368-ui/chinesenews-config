#!/usr/bin/env python3
"""
Generate `daily-news.json` (Tier 0 curated feed) for the Chinese News app.

A NEUTRAL, simplified-Chinese daily reader for learners. It gathers from several
sources, pulls each article body, strips footer/noise, keeps 150..1500-hanzi
articles, drops anything political (is_neutral), removes anything shown in the
last few days (seen.json), and emits ~20 fresh items as one JSON file. Run daily
from GitHub Actions — the apps read it first (Tier 0) and skip scraping entirely.

Sources (all simplified Chinese):
  • VOA 中文 — NEUTRAL sections only (科教/经济), public domain
  • IT之家 / 36氪 / 少数派 — consumer tech, business, digital life (scraped)
  • 维基新闻 (Wikinews) — factual, CC BY 2.5

Why these: VOA alone skews political on busy news days (most items get filtered
out), so the tech/business RSS sources keep the feed full AND apolitical. The
neutrality keyword filter is the safety net; seen.json guarantees no day-to-day
repeats (replacing the old yesterday-backfill that CAUSED them).

Stdlib only (urllib/re/json/html) so the GitHub Action needs no `pip install`.

Usage:
    python generate_daily_news.py --out daily-news.json --target 20 --seen seen.json
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

# VOA category landing pages — NEUTRAL sections ONLY (science/tech/culture/
# health + economy/finance). The political sections (焦点/印太/全球议题/中东/中国/
# 美国/国际/时事大家谈) are deliberately excluded so the learner feed stays apolitical.
# A keyword filter (is_neutral) then drops anything political that still slips in.
VOA_SOURCES = [
    "https://www.voachinese.com/z/5679",  # 科教·文娱·体健 (sci/edu/culture/sport/health)
    "https://www.voachinese.com/z/1748",  # 经济·金融·贸易 (economy/finance/trade)
    "https://www.voachinese.com/z/1762",  # 经济 (legacy economy)
]
VOA_ALLOWED_HOSTS = {"www.voachinese.com", "voachinese.com"}
WIKINEWS_ALLOWED_HOSTS = {"zh.wikinews.org", "wikinews.org"}

# Neutral, simplified-Chinese RSS sources, scraped for full article bodies
# (reusing extract_main_text). Each is apolitical by nature — consumer tech,
# business, digital life — a reliable counterweight to VOA on political-news
# days (when most VOA items get dropped by the neutrality filter).
# Tuple: (source, feed_url, {allowed hosts}, (skip-title substrings), max_share)
_SSPAI_SKIP = ("众测", "招募", "线下活动", "社区速递", "一图流", "派活动",
               "上新", "限时", "优惠", "抽奖", "共创")
_36KR_SKIP = ("氪星晚报", "36氪音频", "8点1氪")   # link-roundup / audio digests
RSS_SOURCES = [
    ("ithome", "https://www.ithome.com/rss/", {"www.ithome.com", "ithome.com"}, (), 8),
    ("36kr",   "https://www.36kr.com/feed",   {"36kr.com", "www.36kr.com"}, _36KR_SKIP, 5),
    ("sspai",  "https://sspai.com/feed",      {"sspai.com", "www.sspai.com"}, _SSPAI_SKIP, 6),
]

# Every published article's body is truncated to this many CJK chars (at a line
# boundary) — a sensible reading-session length, independent of the acceptance
# window used to QUALIFY the article.
DISPLAY_MAX_HANZI = 1200

# Cross-day dedup: a rolling {identity: first-seen-date} map, persisted on the
# feed branch as seen.json. Anything published in the last N days is skipped, so
# "어제 기사가 오늘 또" can't happen — replaces the old yesterday-backfill that
# CAUSED the repeats.
SEEN_PATH = "seen.json"
SEEN_RETENTION_DAYS = 10

# Neutrality filter — keep the feed tech/econ/sci/culture/health, drop overtly
# political or sensitive items. Matched against the TITLE + a short body prefix
# (where political framing lives); intentionally conservative so factual market/
# tech stories survive (plain 关税/贸易 are NOT blocked, only their war framings).
_POLITICAL_MARKERS = (
    # leaders / government / elections
    "习近平", "李强", "拜登", "特朗普", "川普", "白宫", "国务院", "总统", "总理",
    "国会", "众议院", "参议院", "大选", "选举", "投票", "议员", "外交部", "政府声明",
    # geopolitics / conflict / coercion
    "战争", "战火", "军事", "导弹", "核武", "武器", "军队", "开战", "停火", "冲突",
    "制裁", "贸易战", "脱钩", "关税战", "抗议", "示威", "镇压", "政变",
    # geopolitics / policy / coercion (US–China tech war, Taiwan, alliances)
    "出口管制", "出口禁令", "芯片出口", "禁止向", "在台协会", "美台", "国防部",
    "五角大楼", "北约", "国务卿", "大使馆", "地缘政治", "自由被",
    # sensitive topics
    "台独", "港独", "新疆", "西藏", "维吾尔", "人权", "民主运动", "独裁",
    "间谍", "异见", "审查", "六四", "法轮",
)


def is_neutral(title, body=""):
    """True when the item looks apolitical (safe for the learner feed)."""
    hay = (title or "") + " " + (body or "")[:200]
    return not any(m in hay for m in _POLITICAL_MARKERS)


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


def truncate_hanzi(text, max_hanzi):
    """Cut `text` to ~`max_hanzi` CJK chars at a line boundary, so a long article
    (e.g. a sspai long-read) becomes a sensible reading length instead of being
    dropped by the upper hanzi gate."""
    if not text or count_cjk(text) <= max_hanzi:
        return text
    kept, n = [], 0
    for line in text.split("\n"):
        kept.append(line)
        n += count_cjk(line)
        if n >= max_hanzi:
            break
    return "\n".join(kept)


def contains_cjk(s):
    return bool(s) and CJK_RE.search(s) is not None


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s)


def decode_entities(s):
    # html.unescape covers all named + numeric (&#NNN; / &#xHH;) entities.
    return html_mod.unescape(s) if s else s


def clean_title(s):
    """First non-empty line with whitespace collapsed. VOA section-page anchors
    wrap the headline AND a teaser, so the raw inner text would otherwise leak
    the teaser (and \\r\\n noise) into the title."""
    if not s:
        return ""
    for line in s.replace("\r", "\n").split("\n"):
        t = " ".join(line.split())
        if t:
            return t
    return ""


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
            title = clean_title(decode_entities(strip_tags(inner)))
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


# ------------------------- generic RSS-scrape source ------------------------

def rss_candidates(feed_url, allowed_hosts, skip_titles, cap):
    """Parse an RSS/Atom feed into {title, link} candidates on the allowed hosts,
    dropping skip-title (community/promo/digest) posts and tracking queries."""
    xml = fetch(feed_url)
    if not xml:
        return []
    out, seen = [], set()
    for block in re.findall(r"<item>(.*?)</item>", xml, re.IGNORECASE | re.DOTALL):
        if len(out) >= cap:
            break
        tm = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.DOTALL)
        lm = re.search(r"<link>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</link>", block, re.DOTALL)
        if not tm or not lm:
            continue
        title = clean_title(decode_entities(strip_tags(tm.group(1))))
        link = lm.group(1).strip().split("?")[0]    # drop ?f=rss-style trackers
        if not title or not contains_cjk(title) or not link:
            continue
        if skip_titles and any(k in title for k in skip_titles):
            continue
        host = re.sub(r"^https?://([^/]+).*$", r"\1", link).lower()
        if (allowed_hosts and host not in allowed_hosts) or link in seen:
            continue
        seen.add(link)
        out.append({"title": title, "link": link})
    return out


def rss_article(cand, lo, hi, source):
    html = fetch(cand["link"])
    if not html:
        return None
    body = truncate_hanzi(sanitize_article_text(extract_main_text(html)), max(hi, DISPLAY_MAX_HANZI))
    hanzi = count_cjk(body)
    if hanzi < lo:   # only a lower gate — truncate_hanzi already capped the top
        return None
    return {"title": cand["title"], "link": cand["link"], "source": source,
            "content": body, "hanziCount": hanzi}


def rss_scrape_articles(source, feed_url, allowed_hosts, skip_titles, cap, lo, hi):
    cands = rss_candidates(feed_url, allowed_hosts, skip_titles, cap * 2)
    out = []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(rss_article, c, lo, hi, source) for c in cands]
        for f in cf.as_completed(futs):
            try:
                r = f.result()
            except Exception:  # noqa: BLE001
                r = None
            if r:
                out.append(r)
    log(f"  {source}: {len(out)} articles")
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

def collect(cap, lo, hi, include_rss):
    """VOA-neutral + (optionally) tech RSS sources + Wikinews, all neutrality-
    filtered, deduped by link+title within the batch, up to `cap` (a surplus the
    caller trims after cross-day dedup). VOA leads when it has neutral news; the
    tech RSS sources keep the feed full on political-news days.

    `include_rss` gates the copyrighted tech sources (ithome/36kr/sspai): when
    False the feed is restricted to redistributable VOA (public domain) +
    Wikinews (CC BY 2.5)."""
    voa = voa_articles(cap, lo, hi)
    rss_batches = []
    if include_rss:
        for source, feed, hosts, skip, share in RSS_SOURCES:
            rss_batches.append(rss_scrape_articles(source, feed, hosts, skip, share, lo, hi)[:share])
    wiki = wikinews_articles(max(10, cap), lo, hi)
    log(f"  collected: voa={len(voa)} " +
        " ".join(f"{RSS_SOURCES[i][0]}={len(b)}" for i, b in enumerate(rss_batches)) +
        f" wikinews={len(wiki)} (filter {lo}..{hi})")

    out, seen_l, seen_t = [], set(), set()
    dropped = []
    for src in [voa, *rss_batches, wiki]:
        for a in src:
            if len(out) >= cap:
                break
            if not is_neutral(a["title"], a.get("content", "")):
                dropped.append(a["title"])
                continue
            if a["link"] in seen_l or a["title"] in seen_t:
                continue
            seen_l.add(a["link"])
            seen_t.add(a["title"])
            out.append(a)
    log(f"  after neutrality filter: kept {len(out)}, dropped {len(dropped)} political")
    for t in dropped:
        log(f"    DROP(political): {t[:50]}")
    return out


# ------------------------------ cross-day dedup -----------------------------

def load_seen(path):
    """{identity: 'YYYY-MM-DD' first-seen} for recently published articles."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:  # noqa: BLE001 - missing/first run is fine
        return {}
    return doc if isinstance(doc, dict) else {}


def prune_seen(seen, today):
    cutoff = (dt.datetime.strptime(today, "%Y-%m-%d")
              - dt.timedelta(days=SEEN_RETENTION_DAYS)).strftime("%Y-%m-%d")
    return {k: v for k, v in seen.items() if isinstance(v, str) and v >= cutoff}


def save_seen(path, seen):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0, sort_keys=True)


# ------------------------------- assemble feed ------------------------------

def build_feed(target, seen, include_rss):
    """Build today's feed from FRESH articles only (nothing in `seen`, i.e. shown
    in the last SEEN_RETENTION_DAYS). Escalate the hanzi window on quiet days to
    still reach `target`. Mutates `seen` to record what we publish."""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    articles = []
    for lo, hi in ((MIN_HANZI, MAX_HANZI), (120, 2200), (80, 4000)):
        # Gather a surplus so cross-day dedup still leaves enough.
        pool = collect(target + 15, lo, hi, include_rss)
        fresh, used_l, used_t = [], set(), set()
        for a in pool:
            link, title = a.get("link", ""), a.get("title", "")
            if link in seen or title in seen:        # shown in the last N days
                continue
            if link in used_l or title in used_t:    # dup within this batch
                continue
            used_l.add(link)
            used_t.add(title)
            fresh.append(a)
            if len(fresh) >= target:
                break
        log(f"pass {lo}..{hi}: {len(fresh)} fresh after cross-day dedup")
        # Keep the BEST pass, not the last — a transient outage (e.g. a Wikinews
        # 429) on a wider pass shouldn't shrink an already-better result.
        if len(fresh) > len(articles):
            articles = fresh
        if len(articles) >= target:
            break

    # Cap each body to a sensible reading length (long sspai/VOA reads → ~1200).
    for a in articles:
        capped = truncate_hanzi(a.get("content", ""), DISPLAY_MAX_HANZI)
        a["content"] = capped
        a["hanziCount"] = count_cjk(capped)

    # Record what we're publishing so tomorrow's run won't repeat it. NO
    # yesterday-backfill: a quiet day yields fewer FRESH articles rather than
    # resurfacing old ones (the app handles <target gracefully).
    for a in articles:
        a.setdefault("publishedDate", today)
        if a.get("link"):
            seen[a["link"]] = today
        if a.get("title"):
            seen[a["title"]] = today

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
    ap.add_argument("--seen", default=SEEN_PATH)
    # By default, restrict to redistributable sources (VOA public-domain +
    # Wikinews CC). --tech-rss adds the copyrighted tech sources for volume.
    ap.add_argument("--tech-rss", action="store_true",
                    help="include copyrighted tech RSS (ithome/36kr/sspai) for reliable 20/day")
    args = ap.parse_args()

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    seen = prune_seen(load_seen(args.seen), today)
    feed = build_feed(args.target, seen, args.tech_rss)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    save_seen(args.seen, seen)
    log(f"WROTE {args.out}: {feed['count']} articles for {feed['date']}; seen={len(seen)}")
    # Hard-fail CI only if we got NOTHING at all — that's a real outage.
    return 0 if feed["count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
