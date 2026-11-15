"""GitHub tools for the chat model: search, browse and read repositories, read profiles.

Uses the public REST API. Without a token GitHub allows 60 requests an hour per IP (a repo
overview costs 3), and code search needs a token at all. Set GITHUB_TOKEN in .env to get
5000 an hour.

The token is used on behalf of EVERY Discord user who talks to the bot, so it must not be
able to see anything private: make it a fine-grained token with repository access set to
"Public repositories (read-only)" and no other permissions. A classic token, or one with
private repo access, would let anyone in chat read the owner's private code.

Like ai_tools, every public function returns text for the model, errors included.
"""
import asyncio
import base64
import json
import os
import re
import urllib.parse

import ai_tools
from ai_tools import ToolError, _clip

API = "https://api.github.com"
SEARCH_RESULTS = 6
README_CHARS = 2500
TREE_PATHS = 70             # file paths shown in a repo overview
FILE_LINES = 160            # lines of code per read, the model pages with start_line
FILE_CHARS = 6000
EVENTS_SHOWN = 12

REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
    r"(?:/(blob|tree)/([^/]+)(?:/(.*))?)?/?(?:[?#].*)?$", re.I)
USER_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([\w-]+)/?(?:[?#].*)?$", re.I)
# github.com/<first segment> pages that aren't users
NOT_USERS = {"about", "features", "pricing", "topics", "trending", "explore", "marketplace",
             "settings", "notifications", "login", "join", "search", "orgs", "sponsors"}


def _headers():
    h = {**ai_tools.BROWSER_HEADERS, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _gh(session, path, params=None):
    """GET an api path, turning github's error shapes into readable ToolErrors."""
    _, status, _, body = await ai_tools.safe_get(session, f"{API}{path}", params=params)
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        data = {}
    if status == 200:
        return data
    message = data.get("message", "") if isinstance(data, dict) else ""
    if status == 404:
        raise ToolError("not found on github (wrong name, or it's private)")
    if status in (403, 429) and "rate limit" in message.lower():
        raise ToolError("github's rate limit is used up for now (60 an hour without a token), try later")
    raise ToolError(f"github said {status}: {message or 'no details'}")


def _session():
    return ai_tools.http_session(headers=_headers())


# github's own naming rules. checked strictly because these go into api url paths, and
# yarl resolves dot segments: a repo of "../.." would otherwise turn /repos/../../user into
# GET /user, authenticated with our token
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _check_segments(value, what):
    """Split a slash path and refuse anything that could climb out of where it belongs."""
    parts = [p for p in value.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ToolError(f"that {what} isn't valid")
    return parts


def _parse_repo(repo):
    """'owner/name', or any github repo/blob/tree url -> (owner, name, ref, path)."""
    repo = (repo or "").strip()
    m = REPO_URL_RE.match(repo)
    if m:
        owner, name, _, ref, path = m.groups()
        path = (path or "").strip("/")
    else:
        m = re.match(r"^([\w.-]+)/([\w.-]+)$", repo)
        if not m:
            raise ToolError("give the repo as owner/name or a github link")
        owner, name, ref, path = m.group(1), m.group(2), None, ""

    if not OWNER_RE.match(owner) or not NAME_RE.match(name) or name in (".", ".."):
        raise ToolError("that isn't a valid github owner/name")
    if ref is not None:
        _check_segments(ref, "branch or tag")
    return owner, name, ref, path


# ---------- search ----------

async def github_search(query: str, kind: str = "repositories"):
    """Searches GitHub. kind is "repositories" (default), "users", "issues" (issues and
    pull requests) or "code".

    Use this to find a project, library or person on GitHub when you don't know the exact
    name. To look inside a result, call github_repo or github_user on it.
    """
    query = (query or "").strip()
    if not query:
        return "no query given"
    kind = (kind or "repositories").lower().strip()
    kind = {"repo": "repositories", "repos": "repositories", "user": "users",
            "issue": "issues", "prs": "issues", "pull requests": "issues"}.get(kind, kind)
    if kind not in ("repositories", "users", "issues", "code"):
        return "kind has to be repositories, users, issues or code"
    if kind == "code" and not os.getenv("GITHUB_TOKEN"):
        return "github code search needs a GITHUB_TOKEN, which isn't set. search repositories instead"

    try:
        async with _session() as session:
            data = await _gh(session, f"/search/{kind}", {"q": query, "per_page": SEARCH_RESULTS})
    except ToolError as e:
        return f"github search failed: {e}"
    except Exception as e:
        print(f"[github] search {type(e).__name__}: {e}")
        return "couldn't reach github"

    items = data.get("items") or []
    if not items:
        return f"no github {kind} for {query!r}"
    lines = []
    for it in items:
        if kind == "repositories":
            lines.append(f"{it['full_name']} ({it.get('stargazers_count', 0)} stars, "
                         f"{it.get('language') or 'no language'})\n{it.get('description') or 'no description'}\n"
                         f"{it.get('html_url')}")
        elif kind == "users":
            lines.append(f"{it['login']} ({it.get('type', 'User')})\n{it.get('html_url')}")
        elif kind == "issues":
            what = "PR" if it.get("pull_request") else "issue"
            repo = it.get("repository_url", "").replace(f"{API}/repos/", "")
            lines.append(f"[{what}, {it.get('state')}] {it.get('title')} in {repo}\n{it.get('html_url')}")
        else:
            lines.append(f"{it['repository']['full_name']}: {it.get('path')}\n{it.get('html_url')}")
    total = data.get("total_count", len(items))
    return f"{total} results, top {len(items)}:\n\n" + "\n\n".join(lines)


# ---------- repositories ----------

async def _repo_overview(session, owner, name, ref):
    repo = await _gh(session, f"/repos/{owner}/{name}")
    ref = ref or repo.get("default_branch") or "HEAD"
    readme_task = _gh(session, f"/repos/{owner}/{name}/readme", {"ref": ref})
    # safe="" so a branch like feature/x is one encoded segment and can't add path levels
    tree_task = _gh(session, f"/repos/{owner}/{name}/git/trees/{urllib.parse.quote(ref, safe='')}", {"recursive": "1"})
    readme, tree = await asyncio.gather(readme_task, tree_task, return_exceptions=True)

    parts = [
        f"{repo['full_name']}: {repo.get('description') or 'no description'}",
        f"{repo.get('stargazers_count', 0)} stars, {repo.get('forks_count', 0)} forks, "
        f"{repo.get('open_issues_count', 0)} open issues, language {repo.get('language') or 'unknown'}, "
        f"license {(repo.get('license') or {}).get('spdx_id') or 'none'}",
        f"default branch {repo.get('default_branch')}, last push {repo.get('pushed_at', '?')[:10]}"
        + (f", topics: {', '.join(repo['topics'])}" if repo.get("topics") else "")
        + (", ARCHIVED" if repo.get("archived") else "") + (", fork" if repo.get("fork") else ""),
        repo.get("html_url", ""),
    ]

    if isinstance(tree, dict):
        entries = tree.get("tree") or []
        files = [e["path"] for e in entries if e.get("type") == "blob"]
        shown = files[:TREE_PATHS]
        more = f"\n... and {len(files) - len(shown)} more files" if len(files) > len(shown) else ""
        parts.append(f"Files ({len(files)}):\n" + "\n".join(shown) + more)

    if isinstance(readme, dict) and readme.get("content"):
        text = base64.b64decode(readme["content"]).decode("utf-8", errors="replace")
        parts.append(f"README:\n{_clip(text, README_CHARS)}")
    else:
        parts.append("(no README)")

    parts.append("To read a file, call github_repo again with its path.")
    return "\n\n".join(parts)


def _numbered(text, start_line):
    lines = text.splitlines()
    total = len(lines)
    start = max(1, min(start_line, total or 1))
    out, size = [], 0
    end = start - 1
    for i in range(start - 1, min(total, start - 1 + FILE_LINES)):
        line = f"{i + 1:>5}  {lines[i]}"
        if size + len(line) > FILE_CHARS and out:
            break
        out.append(line)
        size += len(line) + 1
        end = i + 1
    return "\n".join(out), start, end, total


async def _repo_path(session, owner, name, ref, path, start_line):
    params = {"ref": ref} if ref else None
    # each segment quoted on its own, after ../ and ./ are refused, so the path can only
    # ever point inside this repo's contents
    safe_path = "/".join(urllib.parse.quote(p, safe="") for p in _check_segments(path, "file path"))
    data = await _gh(session, f"/repos/{owner}/{name}/contents/{safe_path}", params)

    if isinstance(data, list):   # a directory
        rows = sorted(data, key=lambda e: (e.get("type") != "dir", e.get("name", "")))
        listing = "\n".join(f"{e['name']}/" if e.get("type") == "dir" else
                            f"{e['name']} ({e.get('size', 0)} bytes)" for e in rows)
        return f"{owner}/{name}/{path}/ contains:\n{listing}"

    if data.get("type") != "file":
        return f"{path} is a {data.get('type')}, not a file or folder"
    if data.get("encoding") != "base64" or not data.get("content"):
        return f"{path} is too big to read through the api ({data.get('size', 0)} bytes)"

    raw = base64.b64decode(data["content"])
    if b"\0" in raw[:4000]:
        return f"{path} is a binary file ({len(raw)} bytes), can't show it as text"
    text = raw.decode("utf-8", errors="replace")
    body, start, end, total = _numbered(text, start_line)
    head = f"{owner}/{name}/{path} (lines {start}-{end} of {total})"
    tail = f"\n\n(more below: call again with start_line={end + 1})" if end < total else ""
    return f"{head}\n\n{body}{tail}"


async def github_repo(repo: str, path: str = "", start_line: int = 1):
    """Looks inside a GitHub repository.

    repo is "owner/name" or a github link. With no path you get the description, stats,
    the list of files and the README. With a path you get that folder's contents, or that
    file's code with line numbers. Long files come back in parts: pass start_line to read
    further. Use this to explain what a project does or how its code works.
    """
    try:
        owner, name, ref, url_path = _parse_repo(repo)
        path = (path or url_path or "").strip().strip("/")
        try:
            start_line = max(1, int(start_line or 1))
        except (TypeError, ValueError):
            start_line = 1
        async with _session() as session:
            if path:
                return await _repo_path(session, owner, name, ref, path, start_line)
            return await _repo_overview(session, owner, name, ref)
    except ToolError as e:
        return f"github failed: {e}"
    except Exception as e:
        print(f"[github] repo {type(e).__name__}: {e}")
        return "couldn't reach github"


# ---------- users ----------

def _event_line(ev):
    kind = ev.get("type", "")
    repo = (ev.get("repo") or {}).get("name", "?")
    p = ev.get("payload") or {}
    date = (ev.get("created_at") or "")[:10]
    if kind == "PushEvent":
        n = p.get("size") or len(p.get("commits") or []) or 1
        branch = (p.get("ref") or "").replace("refs/heads/", "")
        msgs = [c.get("message", "").splitlines()[0] for c in (p.get("commits") or [])[:2]]
        detail = f": {'; '.join(msgs)}" if msgs else ""
        what = f"pushed {n} commit{'s' if n != 1 else ''} to {repo}" + (f" ({branch})" if branch else "") + detail
    elif kind == "PullRequestEvent":
        pr = p.get("pull_request") or {}
        what = f"{p.get('action')} PR \"{pr.get('title', '')}\" in {repo}"
    elif kind == "IssuesEvent":
        what = f"{p.get('action')} issue \"{(p.get('issue') or {}).get('title', '')}\" in {repo}"
    elif kind == "IssueCommentEvent":
        what = f"commented on \"{(p.get('issue') or {}).get('title', '')}\" in {repo}"
    elif kind == "CreateEvent":
        what = f"created {p.get('ref_type')} {p.get('ref') or ''} in {repo}".replace("  ", " ")
    elif kind == "DeleteEvent":
        what = f"deleted {p.get('ref_type')} {p.get('ref')} in {repo}"
    elif kind == "WatchEvent":
        what = f"starred {repo}"
    elif kind == "ForkEvent":
        what = f"forked {repo}"
    elif kind == "ReleaseEvent":
        what = f"released {(p.get('release') or {}).get('tag_name', '')} of {repo}"
    elif kind == "PullRequestReviewEvent":
        what = f"reviewed a PR in {repo}"
    else:
        what = f"{kind.replace('Event', '')} in {repo}"
    return f"{date}: {what}"


async def github_user(username: str):
    """Reads a GitHub profile: bio, stats, their most recently updated repositories, and
    their latest public activity (pushes, pull requests, issues, stars, releases).

    Use this when the user asks about a GitHub account or what someone has been working on.
    username can also be a github profile link.
    """
    username = (username or "").strip().lstrip("@")
    m = USER_URL_RE.match(username)
    if m:
        username = m.group(1)
    if not re.match(r"^[\w-]+$", username) or username.lower() in NOT_USERS:
        return "give a github username"

    try:
        async with _session() as session:
            user, repos, events = await asyncio.gather(
                _gh(session, f"/users/{username}"),
                _gh(session, f"/users/{username}/repos", {"sort": "pushed", "per_page": 6}),
                _gh(session, f"/users/{username}/events/public", {"per_page": 40}),
                return_exceptions=True,
            )
    except Exception as e:
        print(f"[github] user {type(e).__name__}: {e}")
        return "couldn't reach github"
    if isinstance(user, Exception):
        return f"github failed: {user}"

    parts = [f"{user.get('login')}" + (f" ({user['name']})" if user.get("name") else "")
             + f", {user.get('type', 'User')}, joined {str(user.get('created_at', '?'))[:10]}"]
    for label, key in (("Bio", "bio"), ("Company", "company"), ("Location", "location"), ("Website", "blog")):
        if user.get(key):
            parts.append(f"{label}: {user[key]}")
    parts.append(f"{user.get('public_repos', 0)} public repos, {user.get('followers', 0)} followers, "
                 f"following {user.get('following', 0)}\n{user.get('html_url')}")

    if isinstance(repos, list) and repos:
        rows = [f"- {r['name']} ({r.get('stargazers_count', 0)} stars, {r.get('language') or '?'}, "
                f"pushed {str(r.get('pushed_at', '?'))[:10]}): {r.get('description') or 'no description'}"
                for r in repos]
        parts.append("Recently updated repos:\n" + "\n".join(rows))

    if isinstance(events, list):
        if events:
            parts.append("Latest public activity:\n" + "\n".join(_event_line(e) for e in events[:EVENTS_SHOWN]))
        else:
            parts.append("No public activity in the last 90 days.")
    return "\n\n".join(parts)


def github_target(url):
    """For fetch_page: (tool, args) if a url is a github repo/file/profile, else None."""
    if REPO_URL_RE.match(url):
        owner, name, _, _ = _parse_repo(url)
        if owner.lower() not in NOT_USERS:
            return github_repo, (url,)
    m = USER_URL_RE.match(url)
    if m and m.group(1).lower() not in NOT_USERS:
        return github_user, (m.group(1),)
    return None
