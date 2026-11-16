"""Small lookup tools for the chat model: a calculator, currency conversion, dictionaries,
arXiv and song lyrics.

Everything here is keyless. The network ones go through ai_tools' public-only session, and
every tool returns a plain string the model can read (errors included), never raises.

The docstrings are the tool descriptions the model reads, so they say WHEN to use the tool.
"""
import ast
import json
import math
import operator
import re
import urllib.parse
import xml.etree.ElementTree as ET

from ai_tools import ToolError, _clip, get_json, http_session, safe_get

# ---------- calculator ----------
# a whitelist AST walker instead of eval(): only numbers, arithmetic and the math functions
# below can appear, so there is nothing to call or import. the size checks stop the cheap
# denial of service tricks (9**9**9, factorial(10**6)) from pinning a cpu core
MAX_INT_BITS = 4096        # about 1200 decimal digits
MAX_EXPONENT = 10000
MAX_FACTORIAL = 1000

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _factorial(n):
    if n != int(n) or n < 0:
        raise ValueError("factorial only takes whole numbers 0 or more")
    if n > MAX_FACTORIAL:
        raise ValueError(f"factorial is capped at {MAX_FACTORIAL}")
    return math.factorial(int(n))


_FUNCS = {
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "min": min, "max": max, "sum": lambda *xs: sum(xs),
    "exp": math.exp, "ln": math.log, "log": math.log, "log10": math.log10, "log2": math.log2,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "degrees": math.degrees, "radians": math.radians, "hypot": math.hypot,
    "factorial": _factorial, "gcd": math.gcd, "lcm": math.lcm,
    "comb": math.comb, "perm": math.perm,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}


def _check_size(value):
    if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
        raise ValueError("that number is too big to work with")
    return value


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(f"unknown name {node.id!r}")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > MAX_EXPONENT:
                raise ValueError(f"exponents are capped at {MAX_EXPONENT}")
            if isinstance(left, int) and isinstance(right, int) and right > 0 \
                    and left.bit_length() * right > MAX_INT_BITS:
                raise ValueError("that number is too big to work with")
        return _check_size(_BIN_OPS[type(node.op)](left, right))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        func = _FUNCS.get(node.func.id)
        if func is None:
            raise ValueError(f"unknown function {node.func.id!r}")
        args = [_eval(a) for a in node.args]
        if node.func.id in ("comb", "perm") and any(a > MAX_FACTORIAL for a in args):
            raise ValueError(f"{node.func.id} is capped at {MAX_FACTORIAL}")
        return _check_size(func(*args))
    raise ValueError("only numbers, + - * / // % ^, brackets and math functions are allowed")


def _normalize(expression):
    expr = expression.strip().lower()
    expr = (expr.replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-")
            .replace("√", "sqrt").replace("π", "pi"))
    if not re.search(r"[a-z_]\s*\(", expr):
        # 1,000,000 -> 1000000, but never inside a call, where gcd(12,345) means two arguments
        expr = re.sub(r"\b\d{1,3}(?:,\d{3})+\b", lambda m: m.group(0).replace(",", ""), expr)
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"\1/100*", expr)   # 20% of 50
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", expr)          # 15% -> 0.15
    expr = re.sub(r"(\d+)!", r"factorial(\1)", expr)
    return expr


def _format_number(value):
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return f"{value:.12g}"
    return str(value)


def calculator(expression: str):
    """Works out a math expression exactly, so you never have to do arithmetic in your head.

    Use this for ANY calculation, even simple looking ones: sums, percentages, powers,
    square roots, trig, logs, factorials. Supports + - * / // % ^, brackets, constants
    pi/e/tau, and functions like sqrt, cbrt, sin, cos, tan (radians), log, ln, log10,
    factorial, gcd, lcm, comb, perm, round, min, max. "20% of 50" and "5!" work too.
    """
    expression = (expression or "").strip()
    if not expression:
        return "no expression given"
    if len(expression) > 300:
        return "that expression is too long"
    expr = _normalize(expression)
    try:
        result = _eval(ast.parse(expr, mode="eval"))
    except SyntaxError:
        return f"couldn't read {expression!r} as math"
    except ZeroDivisionError:
        return "division by zero, the answer is undefined"
    except (RecursionError, MemoryError):
        return "that expression is nested too deeply"
    except (ValueError, OverflowError, TypeError, ArithmeticError) as e:
        return f"couldn't calculate that: {e}"
    if isinstance(result, complex):
        return f"{expression} has no real-number answer"
    if isinstance(result, float) and not math.isfinite(result):
        return f"{expression} overflows, the result is too big for a float"
    return f"{expression} = {_format_number(result)}"


# ---------- currency (frankfurter: european central bank rates, keyless) ----------
FRANKFURTER = "https://api.frankfurter.dev/v1"
CURRENCY_NAMES = {
    "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollar": "USD", "$": "USD",
    "euro": "EUR", "euros": "EUR", "€": "EUR", "pound": "GBP", "pounds": "GBP", "£": "GBP",
    "sterling": "GBP", "yen": "JPY", "¥": "JPY", "yuan": "CNY", "renminbi": "CNY",
    "rupee": "INR", "rupees": "INR", "₹": "INR", "won": "KRW", "franc": "CHF",
    "swiss franc": "CHF", "canadian dollar": "CAD", "australian dollar": "AUD",
    "real": "BRL", "reais": "BRL", "peso": "MXN", "mexican peso": "MXN", "zloty": "PLN",
    "krona": "SEK", "krone": "NOK", "lira": "TRY", "rand": "ZAR", "forint": "HUF",
    "koruna": "CZK", "shekel": "ILS", "ringgit": "MYR", "baht": "THB", "rupiah": "IDR",
}


def _currency_code(value):
    value = (value or "").strip()
    return CURRENCY_NAMES.get(value.lower(), value.upper())


async def currency_convert(amount: float, source: str, target: str, date: str = ""):
    """Converts money between currencies using the European Central Bank's daily rates.

    Use this whenever the user asks what an amount is worth in another currency, or for an
    exchange rate. source and target are currency codes like USD, EUR, GBP, JPY (common
    names like "euro" or "yen" work too). target can be several codes separated by commas.
    date is optional, YYYY-MM-DD, for a past rate. Crypto isn't covered.
    """
    src = _currency_code(source)
    targets = [_currency_code(t) for t in str(target or "").split(",") if t.strip()]
    if not re.fullmatch(r"[A-Z]{3}", src) or not targets or \
            not all(re.fullmatch(r"[A-Z]{3}", t) for t in targets):
        return "use 3 letter currency codes like USD, EUR or JPY"
    try:
        amount = float(amount if amount not in (None, "") else 1)
    except (TypeError, ValueError):
        return f"{amount!r} isn't a number"
    if not math.isfinite(amount) or abs(amount) > 1e15:
        return "that amount is out of range"
    date = (date or "").strip()
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return "the date has to look like 2024-01-31"
    targets = [t for t in targets if t != src] or targets

    try:
        async with http_session() as session:
            data = await get_json(session, f"{FRANKFURTER}/{date or 'latest'}",
                                  params={"base": src, "symbols": ",".join(targets)})
    except ToolError as e:
        if "404" in str(e) or "422" in str(e):
            return f"frankfurter doesn't know one of {src}, {', '.join(targets)} (it covers ~30 major currencies, no crypto)"
        return f"currency lookup failed: {e}"
    except Exception as e:
        print(f"[currency] {type(e).__name__}: {e}")
        return "couldn't reach the exchange rate service"

    rates = data.get("rates") or {}
    if not rates:
        return "no rates came back for that"
    lines = [f"{amount:,.2f} {src} = {amount * rate:,.2f} {code} (1 {src} = {rate:.6g} {code})"
             for code, rate in rates.items()]
    return "\n".join(lines) + f"\n(ECB reference rate from {data.get('date', 'today')})"


# ---------- dictionaries ----------
DICTIONARY_API = "https://api.dictionaryapi.dev/api/v2/entries/en/"
URBAN_API = "https://api.urbandictionary.com/v0/define"
MAX_SENSES = 6


async def dictionary(word: str, source: str = "dictionary"):
    """Looks up what a word or phrase means.

    source "dictionary" is a proper English dictionary: definitions, part of speech,
    pronunciation, examples and synonyms. source "urban" is Urban Dictionary, for slang,
    memes and internet terms (it's user written, so it can be crude or joking). Use urban
    for slang the normal dictionary won't have, and say it's from Urban Dictionary.
    """
    word = (word or "").strip()[:100]
    if not word:
        return "no word given"
    if "urban" in (source or "").lower():
        return await _urban(word)
    try:
        async with http_session() as session:
            _, status, _, body = await safe_get(session, DICTIONARY_API + urllib.parse.quote(word.lower(), safe=""))
    except Exception as e:
        print(f"[dictionary] {type(e).__name__}: {e}")
        return "couldn't reach the dictionary"
    if status == 404:
        return f"the dictionary has no entry for {word!r}. if it's slang, try source 'urban'"
    if status != 200:
        return f"dictionary lookup failed (http {status})"
    try:
        entries = json.loads(body)
    except ValueError:
        return "the dictionary sent back something unreadable"

    out = []
    phonetic = next((e.get("phonetic") for e in entries if e.get("phonetic")), "")
    out.append(f"{word}" + (f"  {phonetic}" if phonetic else ""))
    senses = 0
    synonyms = []
    for entry in entries:
        for meaning in entry.get("meanings") or []:
            pos = meaning.get("partOfSpeech", "")
            synonyms += meaning.get("synonyms") or []
            for d in meaning.get("definitions") or []:
                if senses >= MAX_SENSES:
                    break
                senses += 1
                line = f"({pos}) {d.get('definition', '')}"
                if d.get("example"):
                    line += f'\n   e.g. "{d["example"]}"'
                out.append(line)
    if synonyms:
        out.append("synonyms: " + ", ".join(dict.fromkeys(synonyms[:8])))
    return "\n".join(out)


async def _urban(term):
    try:
        async with http_session() as session:
            data = await get_json(session, URBAN_API, params={"term": term})
    except Exception as e:
        print(f"[urban] {type(e).__name__}: {e}")
        return "couldn't reach Urban Dictionary"
    entries = data.get("list") or []   # already in urban's own ranking (its vote counts read 0 now)
    if not entries:
        return f"Urban Dictionary has nothing for {term!r}"

    def clean(text):
        return _clip(text.replace("[", "").replace("]", "").replace("\r", ""), 500)

    out = [f"Urban Dictionary, {term!r} (user written):"]
    for d in entries[:3]:
        part = f"- {clean(d.get('definition', ''))}"
        if d.get("example"):
            part += f'\n  e.g. "{clean(d["example"])}"'
        out.append(part)
    return "\n".join(out)


# ---------- arXiv ----------
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)", re.I)
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_STOPWORDS = {"a", "an", "the", "is", "are", "of", "for", "to", "in", "on", "and", "or", "with",
                   "by", "all", "you", "we", "it", "this", "that", "be", "as", "at", "from", "about",
                   "paper", "papers", "research", "latest", "new", "recent", "study", "studies"}


async def arxiv_search(query: str, sort: str = "relevance"):
    """Searches arXiv for scientific papers (physics, math, computer science, AI, biology,
    economics...). Returns titles, authors, dates, abstracts and links.

    Use this when the user asks about research papers, the latest research on a topic, or
    a specific paper. query is keywords, or an arXiv id / link to get that exact paper.
    sort is "relevance" or "newest". To read a paper beyond its abstract, call fetch_page
    with its link.
    """
    query = (query or "").strip()[:300]
    if not query:
        return "no query given"
    id_match = ARXIV_ID_RE.search(query) if ("arxiv" in query.lower() or re.fullmatch(r"[\w./\-v]+", query)) else None
    if id_match:
        attempts = [{"id_list": id_match.group(1)}]
    else:
        # arxiv ANDs every word literally, so "is"/"all"/"you" alone sink a search. drop the
        # filler words, and also match the whole query as a title phrase for exact paper names
        words = [w for w in re.findall(r"[\w\-]+", query.lower()) if w not in ARXIV_STOPWORDS][:10]
        if not words:
            return "the query needs some real keywords"
        phrase = " ".join(re.findall(r"[\w\-]+", query))
        common = {"max_results": 5, "sortOrder": "descending",
                  "sortBy": "submittedDate" if "new" in (sort or "").lower() else "relevance"}
        attempts = [{**common, "search_query": f'ti:"{phrase}"'}] if len(phrase.split()) >= 3 else []
        attempts += [
            {**common, "search_query": " AND ".join(f"all:{w}" for w in words)},
            {**common, "search_query": " OR ".join(f"all:{w}" for w in words)},   # nothing had them all
        ]

    entries = []
    try:
        async with http_session() as session:
            for params in attempts:
                _, status, _, body = await safe_get(session, ARXIV_API, params=params)
                if status != 200:
                    return f"arXiv search failed (http {status})"
                entries = ET.fromstring(body).findall("a:entry", ATOM)
                if entries:
                    break
    except ET.ParseError:
        return "arXiv sent back something unreadable"
    except Exception as e:
        print(f"[arxiv] {type(e).__name__}: {e}")
        return "couldn't reach arXiv"

    # arxiv's relevance puts "Tool Attention Is All You Need" above "Attention Is All You Need",
    # so a title that IS the query goes first
    wanted = re.sub(r"\W+", " ", query.lower()).strip()
    entries.sort(key=lambda e: re.sub(r"\W+", " ", e.findtext("a:title", "", ATOM).lower()).strip() != wanted)

    papers = []
    for entry in entries:
        title = " ".join((entry.findtext("a:title", "", ATOM)).split())
        if not title or title.lower() == "error":
            continue
        authors = [a.findtext("a:name", "", ATOM) for a in entry.findall("a:author", ATOM)]
        author_text = ", ".join(authors[:4]) + (f" + {len(authors) - 4} more" if len(authors) > 4 else "")
        published = entry.findtext("a:published", "", ATOM)[:10]
        link = entry.findtext("a:id", "", ATOM)
        category = entry.find("arxiv:primary_category", ATOM)
        cat = f" [{category.get('term')}]" if category is not None else ""
        abstract = _clip(" ".join(entry.findtext("a:summary", "", ATOM).split()), 600)
        papers.append(f"{title}{cat}\n{author_text} ({published})\n{link}\n{abstract}")
    if not papers:
        return f"no arXiv papers found for {query!r}"
    return "\n\n".join(papers)


# ---------- lyrics (lrclib, keyless) ----------
LRCLIB = "https://lrclib.net/api"
LYRICS_MAX_CHARS = 4000


async def lyrics(song: str, artist: str = ""):
    """Finds the lyrics of a song.

    Use this when the user asks for a song's lyrics, what a line in a song is, or which song
    some lyrics come from. song is the title (or a line of the lyrics), artist is optional
    but makes the match much better.
    """
    song, artist = (song or "").strip()[:200], (artist or "").strip()[:100]
    if not song:
        return "no song given"
    params = {"track_name": song, "artist_name": artist} if artist else {"q": song}
    try:
        async with http_session(headers={"User-Agent": "MeowBot/1.0 (discord bot)"}) as session:
            results = await get_json(session, f"{LRCLIB}/search", params=params)
            if not results and artist:   # the title might be off, try a loose search
                results = await get_json(session, f"{LRCLIB}/search", params={"q": f"{artist} {song}"})
    except Exception as e:
        print(f"[lyrics] {type(e).__name__}: {e}")
        return "couldn't reach the lyrics database"
    if not isinstance(results, list) or not results:
        return f"no lyrics found for {song!r}" + (f" by {artist}" if artist else "")

    best = next((r for r in results if r.get("plainLyrics")), None) or results[0]
    header = f"{best.get('trackName')} by {best.get('artistName')}"
    if best.get("albumName"):
        header += f" (album: {best['albumName']})"
    if best.get("instrumental"):
        return f"{header}\nit's an instrumental, no lyrics"
    text = best.get("plainLyrics") or re.sub(r"\[\d+:\d+\.\d+\]\s?", "", best.get("syncedLyrics") or "")
    if not text.strip():
        return f"{header}\nthe database has this song but no lyrics for it"
    others = [f"{r.get('trackName')} by {r.get('artistName')}" for r in results[1:4]
              if (r.get("trackName"), r.get("artistName")) != (best.get("trackName"), best.get("artistName"))]
    tail = ("\n\n(other matches: " + "; ".join(dict.fromkeys(others)) + ")") if others else ""
    return f"{header}\n\n{_clip(text, LYRICS_MAX_CHARS)}{tail}"
