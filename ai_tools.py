"""Web tools the chat model can call: search, page fetching, news, Stack Overflow, and
loading images for vision.

Nothing in here knows about discord or the bot. Every function returns plain text meant for
the model (errors included, as text, so a failed tool still gives it something to say).

The docstrings of the public tool functions double as the tool descriptions the model
reads (main.py builds the schemas from them), so keep them about WHEN to use the tool.
"""
import asyncio
import base64
import datetime
import html
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError   # windows needs the tzdata package

import aiohttp
import aiohttp.abc
import cv2
import numpy as np
from bs4 import BeautifulSoup


# ---------- config ----------
WEB_SEARCH_RESULTS = 5          # results handed to the model (ddg returns ~10 per page)
WEB_SEARCH_SNIPPET_CHARS = 400  # per result, ddg snippets are short anyway
WEB_SEARCH_RETRY_DELAY = 2      # seconds to wait before the lite fallback after a 202
HTTP_TIMEOUT = 20               # seconds, per tool call
FETCH_MAX_BYTES = 3 * 1024 * 1024    # stop downloading a page past this
FETCH_TEXT_CHARS = 3500         # page text handed to the model, it all becomes prompt
FETCH_MAX_REDIRECTS = 5
NEWS_ITEMS = 8
SO_RESULTS = 5
SO_ANSWERS = 2
SO_ANSWER_CHARS = 1200
HN_COMMENTS = 5
HN_COMMENT_CHARS = 400
IMAGE_MAX_BYTES = 15 * 1024 * 1024
IMAGE_MAX_SIDE = 768            # vision tokens scale with size, and num_ctx is only 8192

BROWSER_HEADERS = {
    # says what it is: wikimedia and a few others reject anonymous generic UAs outright
    "User-Agent": "Mozilla/5.0 (compatible; MeowBot/1.0; discord bot)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SEARCH_HEADERS = {**BROWSER_HEADERS, "User-Agent": "Mozilla/5.0"}   # what ddg was tested with


class ToolError(Exception):
    """A failure whose message is fine to hand straight to the model."""


# ---------- safe fetching ----------
# the model picks these urls, and the model takes orders from whoever is chatting. without
# this, "fetch http://127.0.0.1:11434/api/tags" or a LAN router page would get read back
# into discord. the resolver covers hostnames (including after redirects and dns tricks),
# the literal-ip check covers urls that skip dns entirely

def _is_public_ip(value):
    try:
        ip = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Resolves like normal but refuses anything that lands on a private address."""

    def __init__(self):
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        hosts = await self._inner.resolve(host, port, family)
        public = [h for h in hosts if _is_public_ip(h["host"])]
        if not public:
            raise OSError(f"{host} points at a private address")
        return public

    async def close(self):
        await self._inner.close()


def _check_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("only http and https links work")
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return   # a hostname, the resolver checks it
    if not _is_public_ip(host):
        raise ToolError("that address is private, not fetching it")


def http_session(**kwargs):
    """A session that can only reach the public internet."""
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
    kwargs.setdefault("timeout", aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    kwargs.setdefault("headers", BROWSER_HEADERS)
    return aiohttp.ClientSession(connector=connector, **kwargs)


async def safe_get(session, url, max_bytes=FETCH_MAX_BYTES, params=None):
    """GET with every redirect hop checked. Returns (final_url, status, content_type, body)."""
    if params:
        # baked into the url so they aren't tacked onto every redirect target as well
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for _ in range(FETCH_MAX_REDIRECTS + 1):
        _check_url(url)
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                url = urllib.parse.urljoin(url, resp.headers["Location"])
                continue
            body = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                body += chunk
                if len(body) > max_bytes:
                    break   # keep what we have, a page cut off at 3MB still has its text
            return str(resp.url), resp.status, resp.headers.get("Content-Type", ""), bytes(body)
    raise ToolError("too many redirects")


async def get_json(session, url, params=None):
    _, status, _, body = await safe_get(session, url, params=params)
    if status != 200:
        raise ToolError(f"got http {status}")
    try:
        return json.loads(body)
    except ValueError:
        raise ToolError("the response wasn't json")


def _clip(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "br", "tr", "pre", "blockquote", "section",
              "article", "table", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6"]


def html_to_text(raw_html):
    """Readable text out of a page or an html fragment."""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer",
                     "aside", "form", "iframe", "button"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    # newlines only where blocks end, so inline tags (<code>, <a>, <b>) stay mid-sentence
    # instead of every one landing on a line of its own
    for tag in root.find_all(BLOCK_TAGS):
        tag.append("\n")
    # collapse runs of spaces inside a line but keep its indent, code blocks need it
    lines = (re.sub(r"(?<=\S)[ \t\r\f\v]+", " ", line).rstrip() for line in root.get_text().split("\n"))
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------- web search (duckduckgo html scrape, no key) ----------

def _ddg_real_url(href):
    """DuckDuckGo wraps result links in //duckduckgo.com/l/?uddg=<real url>; unwrap them."""
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        if parsed.path.startswith("/y.js"):   # sponsored result
            return None
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        return target[0] if target else None
    return href


def _parse_ddg_html(raw):
    """Results from html.duckduckgo.com/html/."""
    soup = BeautifulSoup(raw, "html.parser")
    results = []
    for div in soup.select("div.result"):
        if "result--ad" in (div.get("class") or []):
            continue
        link = div.select_one("a.result__a")
        url = _ddg_real_url(link.get("href") if link else None)
        if not url:
            continue
        snippet = div.select_one(".result__snippet")
        results.append({
            "title": link.get_text(" ", strip=True),
            "url": url,
            "snippet": snippet.get_text(" ", strip=True) if snippet else "",
        })
    return results


def _parse_ddg_lite(raw):
    """Results from lite.duckduckgo.com/lite/, a table where each hit spans a few rows."""
    soup = BeautifulSoup(raw, "html.parser")
    results = []
    for link in soup.select("a.result-link"):
        url = _ddg_real_url(link.get("href"))
        if not url:
            continue
        snippet = ""
        row = link.find_parent("tr")
        nxt = row.find_next_sibling("tr") if row else None
        cell = nxt.select_one("td.result-snippet") if nxt else None
        if cell:
            snippet = cell.get_text(" ", strip=True)
        results.append({"title": link.get_text(" ", strip=True), "url": url, "snippet": snippet})
    return results


async def ddg_results(query, region="us-en"):
    """(list of {title, url, snippet}, last_status). The list is None if every attempt failed.

    ddg answers bursts of requests with a 202 "anomaly" page instead of results, so on a
    202 we back off and try the lite endpoint (separately limited) before giving up.
    """
    attempts = [
        ("https://html.duckduckgo.com/html/", _parse_ddg_html),
        ("https://lite.duckduckgo.com/lite/", _parse_ddg_lite),
    ]
    last_status = None
    async with http_session(headers=SEARCH_HEADERS) as session:
        for i, (url, parse) in enumerate(attempts):
            if i:
                await asyncio.sleep(WEB_SEARCH_RETRY_DELAY)
            async with session.post(url, data={"q": query, "kl": region}) as resp:
                raw = await resp.text()
                last_status = resp.status
            if last_status != 200:
                print(f"[search] {url} -> {last_status}")
                continue
            return parse(raw), last_status
    return None, last_status


def searxng_url():
    # read per call: main.py imports this module before it loads .env
    return (os.getenv("SEARXNG_URL") or "").rstrip("/")


SEARCH_CACHE_TTL = 10 * 60      # same query within 10 minutes reuses the results
SEARCH_CACHE_MAX = 200
DDG_COOLDOWN = 2 * 60           # after ddg 202s, leave it alone this long
_search_cache = {}              # (query, region) -> (time, results, source)
_ddg_blocked_until = 0.0


async def searxng_results(query, region="us-en"):
    """Results from your own SearXNG instance. It's configured by you, often on localhost
    or the LAN, so this deliberately skips the public-only resolver. Its settings.yml needs
    json in search.formats, or every request comes back 403."""
    lang = region.split("-")[-1] if "-" in region else "en"
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, headers=SEARCH_HEADERS) as session:
        async with session.get(f"{searxng_url()}/search",
                               params={"q": query, "format": "json", "language": lang}) as resp:
            if resp.status != 200:
                print(f"[search] searxng -> {resp.status}"
                      + (" (is json enabled in its settings.yml?)" if resp.status == 403 else ""))
                return None
            data = await resp.json(content_type=None)
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content") or ""}
            for r in data.get("results") or [] if r.get("url")]


def _ddgs_text(query, region):
    from ddgs import DDGS   # imported here so a broken ddgs install can't stop the bot starting
    rows = DDGS(timeout=HTTP_TIMEOUT).text(query, region=region, safesearch="moderate",
                                           max_results=WEB_SEARCH_RESULTS * 2)
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body") or ""}
            for r in rows or [] if r.get("href")]


async def ddgs_results(query, region="us-en"):
    """The ddgs package: an unofficial metasearch scraper that rotates through several
    engines (bing, brave, google, mojeek, yandex...) on its own, so one engine blocking us
    doesn't end the search. It's synchronous, so it runs in a thread."""
    try:
        return await asyncio.to_thread(_ddgs_text, query, region)
    except Exception as e:
        # its rate limit exception isn't reliably raised, so everything is just "failed"
        print(f"[search] ddgs {type(e).__name__}: {e}")
        return None


async def ddg_instant_results(query):
    """DuckDuckGo's instant answer api: a separate endpoint from the html search, so it
    usually still works while that one is rate limited. Facts and summaries only (from
    wikipedia and friends), no news, so it's the last resort."""
    async with http_session(headers=SEARCH_HEADERS) as session:
        data = await get_json(session, "https://api.duckduckgo.com/",
                              params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"})
    results = []
    if data.get("Answer"):
        results.append({"title": f"Instant answer: {data.get('AnswerType') or 'answer'}",
                        "url": data.get("AbstractURL") or "https://duckduckgo.com", "snippet": str(data["Answer"])})
    if data.get("AbstractText"):
        results.append({"title": data.get("Heading") or query, "url": data.get("AbstractURL", ""),
                        "snippet": data["AbstractText"]})
    for topic in data.get("RelatedTopics") or []:
        for t in topic.get("Topics") or [topic]:   # some come grouped in categories
            if t.get("FirstURL") and t.get("Text"):
                results.append({"title": t["Text"].split(" - ")[0], "url": t["FirstURL"], "snippet": t["Text"]})
    return results


async def search_results(query, region="us-en"):
    """Web results through every backend we have, best first. Returns (results, source).
    results is None only when every backend failed; [] means it worked and found nothing.

    order: cache, your SearXNG (if SEARXNG_URL is set), ddg html then lite (skipped while
    cooling down from a 202), the ddgs package (other engines), then ddg instant answers
    for plain queries.
    """
    global _ddg_blocked_until
    key = (query.lower(), region)
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < SEARCH_CACHE_TTL:
        return hit[1], f"{hit[2]}, cached"

    async def attempt(name, coro):
        try:
            return await coro
        except Exception as e:
            print(f"[search] {name} {type(e).__name__}: {e}")
            return None

    results, source = None, None
    if searxng_url():
        results, source = await attempt("searxng", searxng_results(query, region)), "searxng"

    if results is None and time.time() >= _ddg_blocked_until:
        found = await attempt("ddg", ddg_results(query, region))
        if found:
            results, status = found
            source = "duckduckgo"
            if results is None and status == 202:
                _ddg_blocked_until = time.time() + DDG_COOLDOWN
                print(f"[search] ddg is rate limiting us, skipping it for {DDG_COOLDOWN}s")

    if not results:
        # ddg blocked us, is cooling down, or came back empty: let ddgs try other engines
        more = await ddgs_results(query, region)
        if more:
            results, source = more, "ddgs"

    # instant answers know nothing about site: filters, so those can't use it
    if results is None and not query.lower().startswith("site:"):
        # empty here means "the real search is down and this one knew nothing", which is
        # not the same as the web having no results, so it counts as a failure
        results, source = (await attempt("ddg instant", ddg_instant_results(query))) or None, "ddg instant answers"

    if results is not None:
        _search_cache[key] = (time.time(), results, source)
        while len(_search_cache) > SEARCH_CACHE_MAX:
            _search_cache.pop(next(iter(_search_cache)))
    return results, source


async def web_search_tool(query: str, region: str = "us-en"):
    """Searches the web and returns the top results, each with a title, url and snippet.

    Use this when the answer depends on something you can't already know: news, prices,
    scores, releases, who currently holds some position, or anything that changed recently.
    Put in the query what you would type into a search box.

    If you already know the answer, answer it instead of searching.
    """
    query = query.strip()
    if not query:
        return "no query given"

    results, source = await search_results(query, region)
    if results is None:
        # worded so the model gives up on searching instead of calling it again, which
        # only digs the rate limit deeper
        return ("web search is unavailable right now. don't search again in this reply: answer "
                "with what you already have and tell the user the search is down for a bit")
    if not results:
        return f"no results for {query!r}"

    print(f"[search] {query!r} -> {len(results)} result(s) via {source}")

    chunks = []
    for r in results[:WEB_SEARCH_RESULTS]:
        snippet = r["snippet"][:WEB_SEARCH_SNIPPET_CHARS]
        chunks.append(f"{r['title']}\n{r['url']}\n{snippet}")
    return "\n\n".join(chunks)


# ---------- stack overflow (stackexchange api, keyless: 300 requests/day per ip) ----------
SE_API = "https://api.stackexchange.com/2.3"
SO_QUESTION_RE = re.compile(r"^https?://(?:www\.)?stackoverflow\.com/(?:questions|q)/(\d+)", re.I)


async def stackoverflow_search(query: str):
    """Searches Stack Overflow for programming questions. Returns matching questions with
    their score, whether they have an accepted answer, and a link.

    Use this for coding errors and how-to questions about code. To read a question's
    answers, call fetch_page with its link.
    """
    query = query.strip()
    if not query:
        return "no query given"
    params = {
        "order": "desc", "sort": "relevance", "q": query,
        "site": "stackoverflow", "pagesize": SO_RESULTS,
    }
    try:
        async with http_session() as session:
            data = await get_json(session, f"{SE_API}/search/advanced", params=params)
    except Exception as e:
        print(f"[so] {type(e).__name__}: {e}")
        return "stack overflow search failed"

    items = data.get("items") or []
    if not items:
        return f"no stack overflow questions for {query!r}"
    lines = []
    for q in items:
        tag = "answered" if q.get("is_answered") else "unanswered"
        if q.get("accepted_answer_id"):
            tag = "accepted answer"
        lines.append(f"{html.unescape(q.get('title', ''))}\n{q.get('link')}\n"
                     f"score {q.get('score', 0)}, {q.get('answer_count', 0)} answers, {tag}")
    return "\n\n".join(lines)


async def _stackoverflow_question(session, qid):
    common = {"site": "stackoverflow", "filter": "withbody"}
    q_data, a_data = await asyncio.gather(
        get_json(session, f"{SE_API}/questions/{qid}", params=common),
        get_json(session, f"{SE_API}/questions/{qid}/answers",
                 params={**common, "order": "desc", "sort": "votes", "pagesize": SO_ANSWERS}),
    )
    questions = q_data.get("items") or []
    if not questions:
        raise ToolError("that stack overflow question doesn't exist")
    q = questions[0]
    parts = [f"Question: {html.unescape(q.get('title', ''))} (score {q.get('score', 0)})",
             _clip(html_to_text(q.get("body", "")), 1000)]
    for a in a_data.get("items") or []:
        label = "Accepted answer" if a.get("is_accepted") else "Answer"
        parts.append(f"{label} (score {a.get('score', 0)}):\n"
                     f"{_clip(html_to_text(a.get('body', '')), SO_ANSWER_CHARS)}")
    if len(parts) == 2:
        parts.append("(no answers yet)")
    return "\n\n".join(parts)


# ---------- hacker news ----------
HN_FIREBASE = "https://hacker-news.firebaseio.com/v0"
HN_ALGOLIA = "https://hn.algolia.com/api/v1"
HN_ITEM_RE = re.compile(r"^https?://news\.ycombinator\.com/item\?id=(\d+)", re.I)
HN_LISTS = {"top": "topstories", "new": "newstories", "best": "beststories",
            "ask": "askstories", "show": "showstories"}


def _hn_line(title, url, points, comments, item_id):
    link = url or f"https://news.ycombinator.com/item?id={item_id}"
    return (f"{title}\n{link}\n{points} points, {comments} comments, "
            f"discussion: https://news.ycombinator.com/item?id={item_id}")


async def _hacker_news(session, query):
    if query:
        data = await get_json(session, f"{HN_ALGOLIA}/search",
                              params={"query": query, "tags": "story", "hitsPerPage": NEWS_ITEMS})
        hits = data.get("hits") or []
        if not hits:
            return f"no hacker news stories for {query!r}"
        return "\n\n".join(_hn_line(h.get("title"), h.get("url"), h.get("points", 0),
                                    h.get("num_comments", 0), h.get("objectID")) for h in hits)

    ids = (await get_json(session, f"{HN_FIREBASE}/topstories.json"))[:NEWS_ITEMS]
    items = await asyncio.gather(*(get_json(session, f"{HN_FIREBASE}/item/{i}.json") for i in ids),
                                 return_exceptions=True)
    lines = [_hn_line(it.get("title"), it.get("url"), it.get("score", 0),
                      it.get("descendants", 0), it.get("id"))
             for it in items if isinstance(it, dict)]
    return "Top stories on Hacker News right now:\n\n" + "\n\n".join(lines)


async def _hacker_news_item(session, item_id):
    item = await get_json(session, f"{HN_ALGOLIA}/items/{item_id}")
    parts = [f"{item.get('title') or 'Hacker News item'} ({item.get('points') or 0} points)"]
    if item.get("url"):
        parts.append(f"Link: {item['url']}")
    if item.get("text"):
        parts.append(_clip(html_to_text(item["text"]), 1500))
    comments = [c for c in item.get("children") or [] if c.get("text")][:HN_COMMENTS]
    for c in comments:
        parts.append(f"{c.get('author')}: {_clip(html_to_text(c['text']), HN_COMMENT_CHARS)}")
    return "\n\n".join(parts)


# ---------- google news (rss) ----------
GOOGLE_NEWS_RSS = "https://news.google.com/rss"


async def _google_news(session, query):
    locale = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    if query:
        url, params = f"{GOOGLE_NEWS_RSS}/search", {"q": query, **locale}
    else:
        url, params = GOOGLE_NEWS_RSS, locale
    _, status, _, body = await safe_get(session, url, params=params)
    if status != 200:
        raise ToolError(f"google news returned {status}")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ToolError("google news sent back something that isn't a feed")

    lines = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        source = item.findtext("source") or ""
        date = item.findtext("pubDate") or ""
        # titles come as "headline - Source", the source is listed separately anyway
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        lines.append(f"{title}\n{source}, {date}\n{item.findtext('link') or ''}")
        if len(lines) >= NEWS_ITEMS:
            break
    if not lines:
        return f"no google news results for {query!r}" if query else "google news had no headlines"
    head = f"Google News for {query!r}:" if query else "Top headlines on Google News:"
    return head + "\n\n" + "\n\n".join(lines)


async def news(source: str = "google", query: str = ""):
    """Gets current news headlines with links.

    source is "google" for general news from Google News, or "hackernews" for tech and
    startup news from Hacker News. Leave query empty for today's top stories, or pass a
    query to find stories about something specific.
    """
    source = (source or "google").lower().replace(" ", "")
    query = (query or "").strip()
    try:
        async with http_session() as session:
            if source in ("hackernews", "hn", "ycombinator"):
                return await _hacker_news(session, query)
            return await _google_news(session, query)
    except ToolError as e:
        return f"news failed: {e}"
    except Exception as e:
        print(f"[news] {type(e).__name__}: {e}")
        return "couldn't get the news right now"


# ---------- generic page fetch ----------

async def fetch_page(url: str):
    """Opens a web page and returns its readable text.

    Use this to read a link the user sent, or a search result when its snippet doesn't
    have the answer. Stack Overflow question links come back with the top answers, and
    Hacker News discussion links come back with the top comments.
    """
    url = (url or "").strip()
    if not url:
        return "no url given"
    if "://" not in url:
        url = "https://" + url   # "example.com/page" style, anything else with a scheme is checked

    try:
        async with http_session() as session:
            if m := SO_QUESTION_RE.match(url):
                return await _stackoverflow_question(session, m.group(1))
            if m := HN_ITEM_RE.match(url):
                return await _hacker_news_item(session, m.group(1))

            final, status, ctype, body = await safe_get(session, url)
    except ToolError as e:
        return f"couldn't fetch that: {e}"
    except Exception as e:
        print(f"[fetch] {url}: {type(e).__name__}: {e}")
        return "couldn't reach that page"

    if status != 200:
        return f"that page returned http {status}"
    ctype = ctype.lower()
    if ctype.startswith("image/"):
        return "that link is an image, use view_image on it instead"
    if not any(t in ctype for t in ("html", "xml", "text", "json")) and ctype:
        return f"that link is a {ctype.split(';')[0]} file, not a page I can read"

    text_raw = body.decode("utf-8", errors="replace")
    if "html" in ctype or "<html" in text_raw[:500].lower():
        soup_title = BeautifulSoup(text_raw, "html.parser").title
        title = soup_title.get_text(strip=True) if soup_title else ""
        text = html_to_text(text_raw)
    else:
        title, text = "", text_raw

    if not text.strip():
        return "that page has no readable text (it probably needs javascript)"
    head = f"{title}\n{final}" if title else final
    return f"{head}\n\n{_clip(text, FETCH_TEXT_CHARS)}"


# ---------- wikis (wikipedia + arch wiki, both mediawiki) ----------
WIKIS = {
    "wikipedia": ("https://en.wikipedia.org/w/api.php", "https://en.wikipedia.org/wiki/"),
    "archwiki": ("https://wiki.archlinux.org/api.php", "https://wiki.archlinux.org/title/"),
}
WIKI_RESULTS = 5
WIKI_TEXT_CHARS = 3000
# arch wiki translations are separate pages titled "Page (Language)"
ARCH_LANGS = re.compile(
    r" \((?:العربية|Български|Česky|Dansk|Deutsch|Ελληνικά|Español|Suomi|Français|עברית|Hrvatski|"
    r"Magyar|Bahasa Indonesia|Italiano|日本語|한국어|Lietuvių|Norsk Bokmål|Nederlands|Polski|"
    r"Português|Română|Русский|Slovenčina|Српски|Svenska|ไทย|Türkçe|Українська|Tiếng Việt|"
    r"简体中文|正體中文)\)$")


async def _wiki_search(session, api, query, archwiki):
    async def search(q, what=None):
        params = {"action": "query", "list": "search", "srsearch": q, "srlimit": 10,
                  "format": "json", "formatversion": "2"}
        if what:
            params["srwhat"] = what
        data = await get_json(session, api, params=params)
        return [h["title"] for h in (data.get("query") or {}).get("search") or []]

    if not archwiki:
        return (await search(query))[:WIKI_RESULTS]

    # the arch wiki's own search is dumb (plain srsearch only matches titles, full text
    # ranks random hardware pages first), so ask ddg about the site and keep english pages
    try:
        results, _ = await search_results(f"site:wiki.archlinux.org {query}")
    except Exception as e:
        print(f"[wiki] ddg {type(e).__name__}: {e}")
        results = None
    titles = []
    for r in results or []:
        m = re.match(r"^https://wiki\.archlinux\.org/title/([^?#]+)", r["url"])
        if m:
            t = urllib.parse.unquote(m.group(1)).replace("_", " ")
            if t not in titles and not ARCH_LANGS.search(t) and not t.startswith(("Talk:", "User:")):
                titles.append(t)
    if titles:
        return titles[:WIKI_RESULTS]

    # ddg is rate limited or found nothing: titles for the whole query, then titles for
    # each word (longest first), then full text, deduped
    words = sorted(set(re.findall(r"[\w+-]{3,}", query)), key=len, reverse=True)
    batches = await asyncio.gather(search(query), *(search(w) for w in words[:3]), search(query, "text"))
    titles = []
    for batch in batches:
        for t in batch:
            if t not in titles and not ARCH_LANGS.search(t):
                titles.append(t)
    return titles[:WIKI_RESULTS]


ALPINE_PAGE_RE = re.compile(r"^https://wiki\.alpinelinux\.org/wiki/([^?#]+)")


async def _alpine_wiki(query):
    """The alpine wiki blocks its api with an anti-bot wall but serves plain article
    pages, so pages are found with a ddg site search and read like any other page."""
    results, _ = await search_results(f"site:wiki.alpinelinux.org {query}")
    if results is None:
        return ("the alpine wiki search is unavailable right now (it goes through web search). "
                "don't retry in this reply, answer from what you know")

    pages = []
    for r in results:
        m = ALPINE_PAGE_RE.match(r["url"])
        if m and not m.group(1).startswith(("Talk:", "User:", "Special:")) and r["url"] not in pages:
            pages.append(r["url"])
    if not pages:
        return f"nothing on the alpine wiki for {query!r}"

    text = await fetch_page(pages[0])
    text = re.sub(r"\?__goaway_[^\s]*", "", text)   # the wall's cookie redirect params, noise
    others = [urllib.parse.unquote(ALPINE_PAGE_RE.match(u).group(1)).replace("_", " ") for u in pages[1:WIKI_RESULTS]]
    tail = f"\n\nOther matching articles: {', '.join(others)}" if others else ""
    return f"{text}{tail}"


async def wiki(query: str, site: str = "wikipedia"):
    """Searches a wiki and returns the best matching article's text plus other matching titles.

    site is "wikipedia" for general knowledge (people, places, history, science),
    "archwiki" for the Arch Linux wiki (linux setup, packages, config files, drivers,
    troubleshooting), or "alpinewiki" for the Alpine Linux wiki (apk, OpenRC, alpine
    setup, containers). Pass an exact article title to read that article.
    """
    query = (query or "").strip()
    if not query:
        return "no query given"
    key = (site or "wikipedia").lower().replace(" ", "").replace("-", "")
    if "alpine" in key:
        return await _alpine_wiki(query)
    key = "archwiki" if "arch" in key else "wikipedia"
    api, page_base = WIKIS[key]

    try:
        async with http_session() as session:
            hits = await _wiki_search(session, api, query, key == "archwiki")
            if not hits:
                return f"nothing on {key} for {query!r}"
            # an exact title match beats whatever the search ranked first
            title = next((h for h in hits if h.lower() == query.lower()), hits[0])
            parsed = await get_json(session, api, params={
                "action": "parse", "page": title, "prop": "text", "redirects": "1",
                "disableeditsection": "1", "format": "json", "formatversion": "2",
            })
    except ToolError as e:
        return f"{key} failed: {e}"
    except Exception as e:
        print(f"[wiki] {type(e).__name__}: {e}")
        return f"couldn't reach {key}"

    page = parsed.get("parse") or {}
    soup = BeautifulSoup(page.get("text") or "", "html.parser")
    # infoboxes, reference lists, navboxes and the table of contents are mostly noise
    for junk in soup.select("table.infobox, .reflist, .references, .navbox, .toc, .mw-references-wrap, "
                            ".hatnote, .ambox, .archwiki-template-meta-related-articles-start, sup.reference"):
        junk.decompose()
    text = html_to_text(str(soup))
    real_title = page.get("title", title)
    url = page_base + urllib.parse.quote(real_title.replace(" ", "_"))
    others = [h for h in hits if h != real_title]
    tail = f"\n\nOther matching articles: {', '.join(others)}" if others else ""
    return f"{real_title}\n{url}\n\n{_clip(text, WIKI_TEXT_CHARS)}{tail}"


# ---------- translation ----------
# google's free "gtx" endpoint (keyless, unofficial, what browser extensions use), with
# mymemory as the fallback. mymemory is much worse, so it's only there for when google
# refuses or changes the endpoint
GOOGLE_TRANSLATE = "https://translate.googleapis.com/translate_a/single"
MYMEMORY = "https://api.mymemory.translated.net/get"
TRANSLATE_MAX_CHARS = 4000
LANGUAGE_CODES = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
    "portuguese": "pt", "brazilian portuguese": "pt", "dutch": "nl", "russian": "ru",
    "ukrainian": "uk", "polish": "pl", "czech": "cs", "swedish": "sv", "norwegian": "no",
    "danish": "da", "finnish": "fi", "greek": "el", "turkish": "tr", "arabic": "ar",
    "hebrew": "he", "persian": "fa", "farsi": "fa", "hindi": "hi", "bengali": "bn",
    "urdu": "ur", "punjabi": "pa", "tamil": "ta", "telugu": "te", "thai": "th",
    "vietnamese": "vi", "indonesian": "id", "malay": "ms", "filipino": "tl", "tagalog": "tl",
    "chinese": "zh-CN", "simplified chinese": "zh-CN", "traditional chinese": "zh-TW",
    "mandarin": "zh-CN", "cantonese": "zh-TW", "japanese": "ja", "korean": "ko",
    "romanian": "ro", "hungarian": "hu", "bulgarian": "bg", "serbian": "sr", "croatian": "hr",
    "slovak": "sk", "slovenian": "sl", "lithuanian": "lt", "latvian": "lv", "estonian": "et",
    "irish": "ga", "welsh": "cy", "latin": "la", "swahili": "sw", "afrikaans": "af",
    "catalan": "ca", "basque": "eu", "galician": "gl", "icelandic": "is", "esperanto": "eo",
}


def _lang_code(lang, default):
    lang = (lang or "").strip()
    if not lang:
        return default
    return LANGUAGE_CODES.get(lang.lower(), lang)


async def translate(text: str, target: str = "english", source: str = "auto"):
    """Translates text from one language to another and says which language it was in.

    Use this when the user asks to translate something, or sends a message in a language
    they want explained. target is the language to translate into (a name like "japanese"
    or a code like "ja"). source is the original language, or leave it as "auto" to detect it.
    """
    text = (text or "").strip()
    if not text:
        return "nothing to translate"
    if len(text) > TRANSLATE_MAX_CHARS:
        text = text[:TRANSLATE_MAX_CHARS]
    tl = _lang_code(target, "en")
    sl = _lang_code(source, "auto")
    # google hands unknown targets back untranslated instead of erroring, so catch them here
    code_re = r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$"
    if not re.match(code_re, tl):
        return f"don't know the language {target!r}, use its english name or a code like 'ja'"
    if sl != "auto" and not re.match(code_re, sl):
        return f"don't know the language {source!r}, use its english name, a code, or 'auto'"

    try:
        async with http_session(headers=SEARCH_HEADERS) as session:
            try:
                data = await get_json(session, GOOGLE_TRANSLATE, params={
                    "client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": text})
                translated = "".join(part[0] for part in data[0] if part and part[0])
                detected = data[2] if len(data) > 2 and isinstance(data[2], str) else sl
                if translated:
                    return f"Translated from {detected} to {tl}:\n{translated}"
            except (ToolError, IndexError, TypeError, KeyError) as e:
                print(f"[translate] google failed: {e}")

            # mymemory has no real auto-detect, so guess english when it wasn't given
            pair = f"{'en' if sl == 'auto' else sl}|{tl}"
            data = await get_json(session, MYMEMORY, params={"q": text[:500], "langpair": pair})
            translated = (data.get("responseData") or {}).get("translatedText")
            if data.get("responseStatus") == 200 and translated:
                return f"Translated ({pair}, backup translator, may be rough):\n{translated}"
            return f"translation failed: {data.get('responseDetails') or 'no details'}"
    except ToolError as e:
        return f"translation failed: {e}"
    except Exception as e:
        print(f"[translate] {type(e).__name__}: {e}")
        return "couldn't reach the translator right now"


# ---------- places, time, weather (open-meteo, keyless) ----------
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "light freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "light snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


async def _geocode(session, place):
    """'tokyo' / 'paris, france' -> the best open-meteo match, or raise ToolError."""
    name = place.split(",")[0].strip()
    hint = place.split(",", 1)[1].strip().lower() if "," in place else ""
    data = await get_json(session, GEOCODE_URL, params={"name": name, "count": 10, "language": "en"})
    results = data.get("results") or []
    if hint:   # "springfield, illinois" -> prefer the one whose region/country matches
        matching = [r for r in results if hint in f"{r.get('admin1', '')} {r.get('country', '')} {r.get('country_code', '')}".lower()]
        results = matching or results
    if not results:
        raise ToolError(f"couldn't find a place called {place!r}")
    return results[0]


def _place_name(r):
    parts = []
    for p in (r.get("name"), r.get("admin1"), r.get("country")):
        if p and p not in parts:   # "Tokyo, Tokyo, Japan" -> "Tokyo, Japan"
            parts.append(p)
    return ", ".join(parts)


def _format_time(now):
    offset = now.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return f"{now.strftime('%A %d %B %Y, %H:%M')} ({now.tzname()}, {offset})"


async def current_time(location: str = ""):
    """Gets the current date and time, either in UTC or in a city, country or timezone.

    Use this when the user asks what time or what day it is, somewhere or in general, or
    when you need today's date to answer something.
    """
    location = (location or "").strip()
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    if not location:
        return f"Current time: {_format_time(utc_now)}"

    # an actual zone name ("Europe/London", "UTC") works directly
    try:
        zone = ZoneInfo(location.replace(" ", "_"))
        return f"Current time in {location}: {_format_time(utc_now.astimezone(zone))}"
    except (ZoneInfoNotFoundError, ValueError):
        pass

    try:
        async with http_session() as session:
            place = await _geocode(session, location)
    except ToolError as e:
        return str(e)
    except Exception as e:
        print(f"[time] {type(e).__name__}: {e}")
        return "couldn't look that place up right now"
    try:
        zone = ZoneInfo(place.get("timezone") or "UTC")
    except ZoneInfoNotFoundError:
        return f"don't know the timezone for {_place_name(place)}"
    return f"Current time in {_place_name(place)}: {_format_time(utc_now.astimezone(zone))}"


def _temp(c):
    # round() first, or -0.3 prints as "-0"
    return f"{round(c) + 0}°C / {round(c * 9 / 5 + 32) + 0}°F"


async def weather(location: str, days: int = 1):
    """Gets the current weather and the forecast for a place.

    Use this when the user asks about weather, temperature, rain or what to wear somewhere.
    days is how many days of forecast to include, from 1 (today) to 7.
    """
    location = (location or "").strip()
    if not location:
        return "which place? the weather tool needs a location"
    try:
        days = max(1, min(7, int(days or 1)))
    except (TypeError, ValueError):
        days = 1

    try:
        async with http_session() as session:
            place = await _geocode(session, location)
            data = await get_json(session, FORECAST_URL, params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                           "precipitation,weather_code,wind_speed_10m,is_day",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max",
                "timezone": "auto", "forecast_days": days,
            })
    except ToolError as e:
        return str(e)
    except Exception as e:
        print(f"[weather] {type(e).__name__}: {e}")
        return "couldn't get the weather right now"

    cur = data.get("current") or {}
    lines = [f"Weather in {_place_name(place)} (local time {cur.get('time', '?').replace('T', ' ')}):"]
    if cur:
        lines.append(
            f"Now: {WMO_CODES.get(cur.get('weather_code'), 'unknown conditions')}, "
            f"{_temp(cur['temperature_2m'])} (feels like {_temp(cur['apparent_temperature'])}), "
            f"humidity {cur.get('relative_humidity_2m')}%, "
            f"wind {cur.get('wind_speed_10m', 0):.0f} km/h ({cur.get('wind_speed_10m', 0) * 0.621:.0f} mph), "
            f"precipitation {cur.get('precipitation', 0)} mm"
        )
    daily = data.get("daily") or {}
    for i, day in enumerate(daily.get("time") or []):
        rain = (daily.get("precipitation_probability_max") or [None] * (i + 1))[i]
        lines.append(
            f"{day}: {WMO_CODES.get(daily['weather_code'][i], 'unknown')}, "
            f"high {_temp(daily['temperature_2m_max'][i])}, low {_temp(daily['temperature_2m_min'][i])}"
            + (f", {rain}% chance of rain" if rain is not None else "")
        )
    return "\n".join(lines)


# ---------- sports (espn's public scoreboard api, unofficial but keyless) ----------
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
SPORTS_LEAGUES = {
    "nfl": "football/nfl",
    "college-football": "football/college-football",
    "nba": "basketball/nba",
    "wnba": "basketball/wnba",
    "college-basketball": "basketball/mens-college-basketball",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
    "premier-league": "soccer/eng.1",
    "la-liga": "soccer/esp.1",
    "bundesliga": "soccer/ger.1",
    "serie-a": "soccer/ita.1",
    "ligue-1": "soccer/fra.1",
    "mls": "soccer/usa.1",
    "champions-league": "soccer/uefa.champions",
    # rugby leagues are numeric ids on espn (from sports.core.api.espn.com/v2/sports/rugby/leagues)
    "nations-championship": "rugby/17567",     # the autumn/summer internationals since 2026
    "six-nations": "rugby/180659",
    "rugby-world-cup": "rugby/164205",
    "rugby-championship": "rugby/244293",
    "rugby-tests": "rugby/289234",             # other international test matches
    "premiership-rugby": "rugby/267979",
    "urc": "rugby/270557",
    "top-14": "rugby/270559",
    "champions-cup": "rugby/271937",
    "super-rugby": "rugby/242041",
    "major-league-rugby": "rugby/289262",
}
LEAGUE_ALIASES = {
    "football": "nfl", "american football": "nfl", "ncaaf": "college-football",
    "basketball": "nba", "ncaab": "college-basketball", "baseball": "mlb", "hockey": "nhl",
    "epl": "premier-league", "premier league": "premier-league", "soccer": "premier-league",
    "laliga": "la-liga", "la liga": "la-liga", "serie a": "serie-a", "ligue 1": "ligue-1",
    "ucl": "champions-league", "champions league": "champions-league",
    "rugby": "nations-championship", "rugby union": "nations-championship",
    "international rugby": "nations-championship", "nations championship": "nations-championship",
    "6 nations": "six-nations", "six nations": "six-nations", "rwc": "rugby-world-cup",
    "rugby world cup": "rugby-world-cup", "premiership": "premiership-rugby",
    "gallagher prem": "premiership-rugby", "united rugby championship": "urc",
    "top14": "top-14", "super rugby": "super-rugby", "mlr": "major-league-rugby",
}
SPORTS_MAX_GAMES = 10


def _game_state(event):
    comp = (event.get("competitions") or [{}])[0]
    # scoreboard events carry status at the top, team schedule events inside the competition
    return ((event.get("status") or comp.get("status") or {}).get("type")) or {}


def _game_line(event):
    comp = (event.get("competitions") or [{}])[0]
    status = _game_state(event)
    teams = sorted(comp.get("competitors") or [], key=lambda t: t.get("homeAway") != "away")

    def team(t, with_score):
        info = t.get("team") or {}
        # scoreboard: records[].summary and score "102". schedule: record[].displayValue and
        # score {"displayValue": "102"}
        recs = t.get("records") or t.get("record") or [{}]
        record = recs[0].get("summary") or recs[0].get("displayValue") if recs else None
        score = t.get("score", "?")
        if isinstance(score, dict):
            score = score.get("displayValue", "?")
        text = info.get("displayName") or info.get("name") or "?"
        if with_score:
            text += f" {score}"
        elif record:
            text += f" ({record})"
        return text

    state = status.get("state")
    started = state in ("in", "post")
    sep = " vs " if comp.get("neutralSite") else " at "
    line = sep.join(team(t, started) for t in teams) if len(teams) == 2 else event.get("name", "?")
    detail = status.get("detail") or status.get("description") or ""
    if state == "in":
        detail = f"LIVE, {detail}"
    return f"{line}: {detail}"


async def sports(league: str, team: str = ""):
    """Gets live scores, results and upcoming games for a sports league.

    league is one of: nfl, college-football, nba, wnba, college-basketball, mlb, nhl,
    premier-league, la-liga, bundesliga, serie-a, ligue-1, mls, champions-league, and for
    rugby union: nations-championship (international matches like Ireland vs Fiji),
    six-nations, rugby-world-cup, rugby-championship, rugby-tests, premiership-rugby, urc,
    top-14, champions-cup, super-rugby, major-league-rugby.
    Pass a team name to get that team's recent and upcoming games instead of today's.
    Use this instead of web search for any score or fixture in these leagues.
    """
    key = (league or "").strip().lower()
    key = LEAGUE_ALIASES.get(key, key.replace(" ", "-"))
    path = SPORTS_LEAGUES.get(key)
    if not path:
        return f"don't have {league!r}. supported leagues: {', '.join(SPORTS_LEAGUES)}"
    team = (team or "").strip().lower()
    base = ESPN_SCOREBOARD.format(path=path).rsplit("/", 1)[0]

    try:
        # espn 403s anything with "bot" in the user agent
        async with http_session(headers=SEARCH_HEADERS) as session:
            if not team:
                data = await get_json(session, f"{base}/scoreboard")
            else:
                # the scoreboard refuses date ranges, so a team goes through its own schedule
                teams_data = await get_json(session, f"{base}/teams", params={"limit": 1000})
                leagues = ((teams_data.get("sports") or [{}])[0].get("leagues") or [{}])
                found = None
                for entry in (leagues[0].get("teams") or []):
                    info = entry.get("team") or {}
                    names = " ".join(str(info.get(k, "")) for k in
                                     ("displayName", "shortDisplayName", "name", "abbreviation",
                                      "location", "nickname"))
                    if team in names.lower():
                        found = info
                        break
                if not found:
                    return f"couldn't find a {key} team matching {team!r}"
                sched_url = f"{base}/teams/{found['id']}/schedule"
                try:
                    # soccer schedules only list played games unless fixtures are asked for too
                    results = await get_json(session, sched_url)
                    events = list(results.get("events") or [])
                    if path.startswith("soccer/"):
                        fixtures = await get_json(session, sched_url, params={"fixture": "true"})
                        seen = {e.get("id") for e in events}
                        events += [e for e in fixtures.get("events") or [] if e.get("id") not in seen]
                except ToolError:
                    # espn's schedule endpoint 500s for rugby, so take the league's current
                    # scoreboard and keep this team's games
                    board = await get_json(session, f"{base}/scoreboard")
                    team_id = str(found.get("id"))
                    events = [e for e in board.get("events") or []
                              if any(str((c.get("team") or {}).get("id")) == team_id
                                     for c in (e.get("competitions") or [{}])[0].get("competitors") or [])]
    except ToolError as e:
        return f"sports failed: {e}"
    except Exception as e:
        print(f"[sports] {type(e).__name__}: {e}")
        return "couldn't get scores right now"

    if team:
        if not events:
            return f"no {key} games found for {found.get('displayName', team)}"
        events.sort(key=lambda e: e.get("date", ""))
        # most relevant first: whatever is live, then the latest results, then what's next
        live = [e for e in events if _game_state(e).get("state") == "in"]
        done = [e for e in events if _game_state(e).get("state") == "post"][-3:]
        upcoming = [e for e in events if _game_state(e).get("state") == "pre"][:3]
        events = live + done + upcoming
        head = f"{key.upper()} games for {found.get('displayName', team)} (recent results, then upcoming):"
    else:
        events = data.get("events") or []
        day = (data.get("day") or {}).get("date")
        if not events:
            return f"no {key} games on {day or 'today'}"
        head = f"{key.upper()} scoreboard for {day or 'today'}:"

    lines = [_game_line(ev) for ev in events[:SPORTS_MAX_GAMES]]
    if len(events) > SPORTS_MAX_GAMES:
        lines.append(f"(+{len(events) - SPORTS_MAX_GAMES} more games)")
    return head + "\n" + "\n".join(lines)


# ---------- local documents (extra_stuff/) ----------
# plain keyword search (bm25) over passages of the files in DOCS_DIR. no embedding model,
# so it matches words, not meaning: "feline" won't find "cat". the index is rebuilt
# whenever a file is added, removed or changed, so dropping a file in is all it takes
DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extra_stuff")
DOC_EXTENSIONS = {".txt", ".md", ".pdf", ".html", ".htm"}
DOC_CHUNK_CHARS = 900       # passage size, roughly a couple of paragraphs
DOC_RESULTS = 4             # passages handed to the model per search
DOC_MAX_FILE_BYTES = 30 * 1024 * 1024
STOPWORDS = set("""a an and are as at be but by for from has have he her his i in is it its of
on or she so that the their them they this to was were what when where which who why will
with you your we our not no do does did can could would should about into than then there""".split())


def _tokens(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 1]


def _read_doc(path):
    """A file -> list of (page_or_None, text)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        from pypdf import PdfReader   # only loaded when there's actually a pdf
        reader = PdfReader(path)
        return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    if ext in (".html", ".htm"):
        raw = html_to_text(raw)
    return [(None, raw)]


def _chunk(pages):
    """Pages -> passages of about DOC_CHUNK_CHARS, split on paragraph breaks."""
    chunks = []
    for page, text in pages:
        buf = ""
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para:
                continue
            if buf and len(buf) + len(para) > DOC_CHUNK_CHARS:
                chunks.append((page, buf))
                buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
            while len(buf) > DOC_CHUNK_CHARS * 2:   # one giant paragraph (pdfs love these)
                chunks.append((page, buf[:DOC_CHUNK_CHARS]))
                buf = buf[DOC_CHUNK_CHARS:]
        if buf:
            chunks.append((page, buf))
    return chunks


class DocumentIndex:
    def __init__(self, folder):
        self.folder = folder
        self.signature = None
        self.files = []        # names, for listing
        self.passages = []     # (file, page, text, token_counts, length)
        self.doc_freq = {}
        self.avg_len = 1.0
        self._lock = threading.Lock()

    def _scan(self):
        if not os.path.isdir(self.folder):
            return []
        root = os.path.realpath(self.folder)
        found = []
        for dirpath, _, names in os.walk(root):
            for name in names:
                path = os.path.realpath(os.path.join(dirpath, name))
                # realpath + prefix check: a symlink can't smuggle in a file from elsewhere
                if not path.startswith(root + os.sep):
                    continue
                if os.path.splitext(name)[1].lower() not in DOC_EXTENSIONS:
                    continue
                st = os.stat(path)
                if st.st_size <= DOC_MAX_FILE_BYTES:
                    found.append((os.path.relpath(path, root), st.st_mtime, st.st_size))
        return sorted(found)

    def refresh(self):
        """Rebuild if anything in the folder changed since last time. Blocking, run in a thread."""
        with self._lock:   # parallel tool calls each run this in their own thread
            self._refresh()

    def _refresh(self):
        found = self._scan()
        if found == self.signature:
            return
        passages, doc_freq = [], {}
        for rel, _, _ in found:
            try:
                pages = _read_doc(os.path.join(self.folder, rel))
            except Exception as e:
                print(f"[docs] couldn't read {rel}: {type(e).__name__}: {e}")
                continue
            for page, text in _chunk(pages):
                toks = _tokens(text)
                if not toks:
                    continue
                counts = {}
                for t in toks:
                    counts[t] = counts.get(t, 0) + 1
                for t in counts:
                    doc_freq[t] = doc_freq.get(t, 0) + 1
                passages.append((rel, page, text, counts, len(toks)))
        self.signature = found
        self.files = [rel for rel, _, _ in found]
        self.passages = passages
        self.doc_freq = doc_freq
        self.avg_len = (sum(p[4] for p in passages) / len(passages)) if passages else 1.0
        print(f"[docs] indexed {len(self.files)} file(s), {len(passages)} passage(s)")

    def search(self, query, k=DOC_RESULTS, k1=1.5, b=0.75):
        terms = set(_tokens(query))
        n = len(self.passages)
        scored = []
        for rel, page, text, counts, length in self.passages:
            score = 0.0
            for t in terms:
                tf = counts.get(t)
                if not tf:
                    continue
                df = self.doc_freq.get(t, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / self.avg_len))
            # naming the file in the query counts for something too
            if terms & set(_tokens(rel)):
                score += 1.0
            if score > 0:
                scored.append((score, rel, page, text))
        scored.sort(key=lambda s: s[0], reverse=True)
        return scored[:k]


DOCS = DocumentIndex(DOCS_DIR)


async def search_documents(query: str):
    """Searches the bot's own document library: research papers and other files the owner
    put in it. Returns the most relevant passages, each labelled with its file and page.

    Use this when the user asks about "the paper", "the docs", a file by name, or a topic
    those documents cover. Pass an empty query to list which documents exist.
    """
    await asyncio.to_thread(DOCS.refresh)
    if not DOCS.files:
        return "the document library is empty"
    query = (query or "").strip()
    if not query:
        return "Documents in the library:\n" + "\n".join(f"- {f}" for f in DOCS.files)

    hits = DOCS.search(query)
    if not hits:
        return (f"nothing in the documents matched {query!r}. the library has: "
                + ", ".join(DOCS.files))
    parts = []
    for _, rel, page, text in hits:
        where = f"{rel}, page {page}" if page else rel
        parts.append(f"[{where}]\n{_clip(text, DOC_CHUNK_CHARS + 200)}")
    return "\n\n---\n\n".join(parts)


# ---------- vision ----------

def prepare_image(data):
    """Raw image bytes -> base64 jpeg, shrunk so it doesn't eat the whole context."""
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ToolError("couldn't decode that image (gifs and svgs don't work)")
    h, w = img.shape[:2]
    scale = IMAGE_MAX_SIDE / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ToolError("couldn't re-encode that image")
    return base64.b64encode(buf.tobytes()).decode("ascii")


async def download_image(url):
    """Fetch an image url (public internet only) and return its raw bytes."""
    async with http_session() as session:
        _, status, ctype, body = await safe_get(session, url, max_bytes=IMAGE_MAX_BYTES)
    if status != 200:
        raise ToolError(f"the image link returned http {status}")
    if ctype and not ctype.lower().startswith("image/"):
        raise ToolError(f"that link is {ctype.split(';')[0]}, not an image")
    return body
