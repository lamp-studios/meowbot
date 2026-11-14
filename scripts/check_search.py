"""Check which web search backends work from this machine.

Run from the repo root:  uv run python scripts/check_search.py ["optional query"]

Search engines block IPs individually (datacenter and VPS ranges especially), so a backend
that works on one machine can be dead on another. This tries each one on its own and
prints what came back, so you can see what the bot actually has to work with here.
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dotenv  # noqa: E402

dotenv.load_dotenv(os.path.join(ROOT, ".env"))

import ai_tools  # noqa: E402

QUERY = sys.argv[1] if len(sys.argv) > 1 else "ireland vs fiji rugby score"
DDGS_ENGINES = ["yahoo", "brave", "mojeek", "startpage", "duckduckgo", "wikipedia", "grokipedia", "auto"]


def show(name, started, results=None, error=None):
    took = f"{time.time() - started:4.1f}s"
    if error:
        print(f"  FAIL  {name:<22} {took}  {error}")
    elif not results:
        print(f"  EMPTY {name:<22} {took}  worked but returned nothing")
    else:
        print(f"  OK    {name:<22} {took}  {len(results)} results, first: {results[0]['url'][:70]}")


async def main():
    print(f"query: {QUERY!r}\n")

    url = ai_tools.searxng_url()
    if url:
        t = time.time()
        try:
            r = await ai_tools.searxng_results(QUERY)
            show(f"searxng ({url})", t, r, None if r is not None else "bad response, see the log line above")
        except Exception as e:
            show("searxng", t, error=f"{type(e).__name__}: {e}")
    else:
        print("  SKIP  searxng                       SEARXNG_URL isn't set in .env")

    for name, endpoint, parse in [
        ("duckduckgo html", "https://html.duckduckgo.com/html/", ai_tools._parse_ddg_html),
        ("duckduckgo lite", "https://lite.duckduckgo.com/lite/", ai_tools._parse_ddg_lite),
    ]:
        t = time.time()
        try:
            async with ai_tools.http_session(headers=ai_tools.SEARCH_HEADERS) as s:
                async with s.post(endpoint, data={"q": QUERY, "kl": "us-en"}) as resp:
                    body, status = await resp.text(), resp.status
            if status != 200:
                show(name, t, error=f"http {status}" + (" (rate limited / ip flagged)" if status == 202 else ""))
            else:
                show(name, t, parse(body))
        except Exception as e:
            show(name, t, error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(1)

    from ddgs import DDGS
    for engine in DDGS_ENGINES:
        t = time.time()
        try:
            rows = await asyncio.to_thread(lambda: DDGS(timeout=15).text(QUERY, backend=engine, max_results=5))
            show(f"ddgs {engine}", t, [{"url": r.get("href", "")} for r in rows])
        except Exception as e:
            show(f"ddgs {engine}", t, error=f"{type(e).__name__}: {str(e)[:80]}")

    t = time.time()
    try:
        show("ddg instant answers", t, await ai_tools.ddg_instant_results(QUERY))
    except Exception as e:
        show("ddg instant answers", t, error=f"{type(e).__name__}: {e}")

    print("\nOK lines are backends the bot can use from here. If only FAIL/EMPTY show up,"
          "\nthis IP is blocked by them and self-hosted SearXNG is the way out.")


asyncio.run(main())
