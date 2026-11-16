# AGENTS.md

Working notes for AI coding agents (and humans) on MeowBot. Read this before changing anything.

> **Keep this file current.** Every change to the code, dependencies, config or behavior
> must update the matching section here in the same change, and add a line to
> [Change log](#change-log). If something here turns out to be wrong, fix it. A stale
> AGENTS.md is worse than none.

## What this is

MeowBot is a py-cord Discord bot whose chat is driven by a local Ollama model with tool
calling. It searches the web, reads pages, generates images and video, sees images, talks
in voice chat (Kokoro TTS) and listens there (faster-whisper STT), and can keep a
persistent per-user memory. Soggy cat (Caesar) is the mascot.

- Python 3.13, managed with `uv` (`pyproject.toml` + `uv.lock`; `requirements.txt` is kept in sync for non-uv installs).
- Chat model `CHAT_MODEL = "qwen3.5:4b"` (multimodal, does tools). `/definition` uses `DEFINITION_MODEL = "huihui_ai/qwen3.5-abliterated:9b"`.
- `OLLAMA_NUM_CTX = 16384`. Ollama at `OLLAMA_HOST` (default `127.0.0.1:11434`).
- Bot owner id: `1056952213056004118` (set as `owner_id` on the Bot; check with `is_bot_owner`).

## Running and checking

```bash
uv sync                                  # install deps
uv run python main.py                    # run the bot (needs .env and ollama)
uv run python -m py_compile main.py ai_tools.py extra_tools.py github_tools.py skills.py memory_store.py voice_listen.py voice_recv_patch.py
uv run python scripts/check_search.py "query"   # which search backends work from this IP
```

- There is no test suite. Verify changes with `py_compile` plus small throwaway scripts that
  import the module under test (put them outside the repo). `main.py` calls `bot.run(token)`
  at import time, so to import it in a script, stub `discord.Bot.run` first.
- **Pushing to `master` deploys.** The production box runs `scripts/update.sh` from a
  systemd timer: it fast-forwards to the latest commit, re-syncs deps, compiles, and
  restarts the bot. Never push code that doesn't compile. Don't commit unless the owner asks.
- `test2.py`, `test3.py`, `test4.py` are the owner's local scratch files (gitignored). Leave them alone.

## Layout

| File | What lives there |
|---|---|
| `main.py` | The bot: config constants, system prompts, Discord events and slash commands, the image/video generation backends, `ToolRun`, Discord-side tools (polls, reactions), the **tool registry**, `chat_with_tools`, `StatusLine`, reply delivery and message splitting, TTS, voice listening glue, persistent-memory glue. |
| `ai_tools.py` | Network tools: SSRF-safe fetching (`http_session`, `safe_get`, `get_json`), the web search fallback chain, `fetch_page`, news (Google News RSS / Hacker News), Stack Overflow, wikis (Wikipedia / Arch / Alpine), translate, time, weather (Open-Meteo), sports (ESPN), BM25 document search over `extra_stuff/`, image download/prep. |
| `extra_tools.py` | Keyless lookups: `calculator` (whitelisted AST, no `eval`), `currency_convert` (Frankfurter / ECB), `dictionary` (dictionaryapi.dev + Urban Dictionary), `arxiv_search`, `lyrics` (lrclib). |
| `github_tools.py` | GitHub search / repo / file / user tools via the REST API (`GITHUB_TOKEN` optional). Validates owner/name/path segments. |
| `skills.py` + `skills/<name>/SKILL.md` | Skill library. Skills are injected when their triggers match the prompt, or loaded by the model through the `use_skill` tool. |
| `server_config.py` | SQLite (`server_config.db`) per-server settings from `/public_server`: disabled commands/features and channel rules. Cached in memory, defaults to everything on everywhere. |
| `memory_store.py` | SQLite (WAL) persistent memory: settings, per-user history, chat log, saved facts. Off by default. |
| `voice_listen.py` | `ListenSink` (energy VAD, barge-in detection) and faster-whisper transcription. |
| `voice_recv_patch.py` | Runtime monkeypatches that make py-cord 2.8.1 voice receive work with DAVE e2ee. |
| `extra_stuff/` | Documents searchable by `search_documents` (markdown, text, PDF). |
| `scripts/` | `setup.sh` / `setup.bat` / `setup.ps1` installers, `update.sh` + `install-autoupdate.sh` (auto-deploy), `check_search.py`. |

Runtime files (gitignored, never commit): `.env`, `memory.db*`, `server_config.db*`, `gen_usage.*`, `kokoro-v1.0.onnx`, `voices-v1.0.bin`.

## How a chat reply flows

1. Entry points: `on_message` (ping or reply to the bot), `/ask`, and `voice_turn` (speech in VC).
2. `get_history(user_id)` gives the user's history (loaded from SQLite when persistent memory is on).
   The request is built from the history plus channel context (opt-in channels only),
   soggy lore (only when he's mentioned), matching skills, and saved memories.
3. A `StatusLine` message ("thinking...") is posted and becomes the reply later.
4. `chat_with_tools(model, messages, run)` loops up to `TOOL_ROUNDS_MAX` rounds. Each round
   it offers `tool_schemas()`, runs the requested tools **in parallel** (max
   `MAX_CALLS_PER_ROUND`) with `run_tool`, and feeds results back. Viewed images go back as
   a user message with `images`. The whole reply is bounded by `REPLY_TIMEOUT`.
5. `deliver_reply` adds tool notes, links and files, and splits the text with `split_msg`,
   which is code-fence aware and stays under 2000 chars per message.
6. History is trimmed (`MAX_TURNS`), and `persist_turn` saves it if memory is on.

`ToolRun` carries everything tools need for one reply: `user_id`, `upload_limit`, `status`,
`channel`, `message` (the user's message, or None for `/ask`), `author`, plus what tools
produced: `files`, `links`, `new_images`, and per-reply counters.

## The tool registry (adding a tool)

All model tools are entries in `TOOLS` in `main.py`. One entry gives the schema, the
status line label and the dispatch:

```python
Tool(my_module.my_tool, {"query": string("what to look up"), "limit": integer("max results")},
     label=lambda a: f'looking up "{short(a.get("query"))}"...'),
```

- The function's **name and docstring are what the model reads**. Write the docstring to say
  *when* to use the tool. Keep it mostly positive: a wall of "do NOT" makes a small model stop
  calling the tool at all.
- Argument schema helpers: `string(desc, enum)`, `integer`, `number`, `boolean`, `strings` (array of strings).
- Arguments are matched to the function's parameters by name. Missing or empty ones fall back to
  the function's own default. Parameters without a default are marked required. String args are
  coerced to `str`.
- A function that takes `run` and/or `key` gets the `ToolRun` / status line key.
- Sync functions run in `asyncio.to_thread`, async ones are awaited. Return a string for the model.
- Options: `fixed={...}` for arguments the model never sets, `call=` for a custom runner
  `(args, run, key) -> (text, image_b64_or_None)`, `offered=` for tools that only appear sometimes
  (the memory tools use `lambda: MEMORY.enabled()`).
- `use_skill`'s schema comes from `skills.use_skill_schema()`, so it lives in `USE_SKILL`, not `TOOLS`.
- Tools must never raise to the model. Return a readable error string. `run_tool` also catches everything as a backstop.
- If a new tool needs a nudge, add it to `SYSTEM_PROMPT`'s knowledge line (the calculator and currency rules live there).
- Network tools belong in `ai_tools.py` / `extra_tools.py`. Tools that touch Discord state go in `main.py` next to `create_poll`.

## Security rules (keep these)

- **Any URL the model chooses goes through `ai_tools.http_session()` + `safe_get`/`get_json`.** They
  block private, loopback and LAN addresses, including after DNS and on every redirect hop. Never use
  plain `aiohttp`/`requests` for a model-chosen URL.
- Put user or model text into URLs only via `params=` (urlencoded) or `urllib.parse.quote(..., safe="")`.
  Validate anything that becomes a path segment (see `github_tools._check_segments`, which also refuses `.` and `..`).
- **Discord actions taken for a user must respect that user's own permissions**: `user_can(run, "send_polls")`,
  `"add_reactions"`, `"use_external_emojis"`. The bot must never do what the asker couldn't.
- Custom emoji reactions can use any server the bot is in, except servers in `EMOJI_BLOCKED_GUILDS`
  (currently `1436662634538602571`). Their emojis only work inside that server itself.
- Mentions: replies use `AllowedMentions(everyone=False, roles=False, users=True)`. Keep @everyone/role pings off.
- `calculator` is a whitelist AST walker with size caps (`MAX_INT_BITS`, `MAX_EXPONENT`, `MAX_FACTORIAL`).
  Never replace it with `eval`.
- Tool output (web pages, lyrics, Urban Dictionary...) is untrusted and can carry prompt injection.
  That's why actions are permission-checked and capped per reply (`MAX_POLLS`, `MAX_REACTIONS`, gen quotas).
- Persistent memory and its `/persistent_memory` toggle are owner-only. `/reset` wipes the user's DB rows too.
- Never log or commit secrets from `.env`. `memory.db` holds private chat logs.

## Web search

`ai_tools.search_results` tries, in order: cache, SearXNG (if `SEARXNG_URL`), DuckDuckGo
HTML/lite (skipped when `DISABLE_DDG` is set or during a rate-limit cooldown), Firecrawl
keyless, keyless MCP servers (Exa, Parallel, Tavily, You.com), AgentSearch, `ddgs`
(yahoo, then auto; yahoo only when DDG is disabled), Mwmbl, Wikipedia, Wiby, and DDG
instant answers. DDG returns HTTP 202 when the IP is flagged. Self-hosted SearXNG is the
reliable fix. MCP providers that report caps in-band (valid JSON) are detected by limit
words and cooled down (24h for daily or monthly caps).

## Voice

- `/ai_tts [voice] [listen]` joins VC. Replies are read aloud with Kokoro. With `listen`,
  `start_listening` attaches a `ListenSink`.
- `ListenSink` does an energy VAD (`VOICE_SPEECH_RMS`, default 500). After `BARGE_IN_MS`
  of real speech it calls `stop_speaking` (cuts the bot off). When the utterance ends it
  goes to `voice_listen.transcribe` (faster-whisper, `WHISPER_MODEL`/`WHISPER_DEVICE`,
  falls back to CPU on CUDA library errors) and then into `voice_turn`. Recording
  auto-restarts up to `VOICE_MAX_RESTARTS` (5) times if it dies. `voice_status_loop` prints `[voice]` stats every 10s.
- **`voice_recv_patch.py` only applies on py-cord exactly 2.8.1.** It fixes the AEAD extension
  strip, DAVE decrypt order and silence handling, the opus decode crash, the keepalive interval,
  double `stop_recording`, adds a direct decode path (`direct_pcm` sinks skip py-cord's lossy
  jitter buffer, with stream-reset detection), and makes `VoiceClient.stop()` stop only playback
  (2.8.1 also killed recording, which broke barge-in). Upgrading py-cord turns the patch off.
  Re-test voice receive before bumping it. Upstream fix: py-cord PR #3159.
- `davey` is pinned `<0.2`. Linux gets CUDA libs for whisper from the `nvidia-cublas-cu12` /
  `nvidia-cudnn-cu12` wheels, preloaded by `_preload_cuda_libs`.

## "Describe this user" (user context menu)

`describe` sends `profile_facts(member)` to the model with `DESCRIBE_SYSTEM_PROMPT` (no
tools, `think=False`). `profile_facts` does one `bot.fetch_user` for banner and accent
colour, then collects badges (`BADGE_NAMES` from `public_flags`), server tag
(`primary_guild`), decorations, and, when it's a `Member`, nickname, join date, ownership,
boosting, roles (highest first, capped at `MAX_PROFILE_ROLES`), name colour, notable
permissions (`NOTABLE_PERMISSIONS`), status, devices and activities (`describe_activity`).
Bots cannot read About Me / bios, or Nitro and boost badges. Discord doesn't expose them
to bot accounts, so don't try (a user token would break Discord's ToS). Role names and
statuses are user-written, and the prompt tells the model to treat them as data.

## /public_server (per-server setup)

Server-owner-only, ephemeral, guild-install only, hidden from non-admins via
`default_member_permissions`. `ServerConfigView` has two multi-selects (ticked = ON):
slash commands, and "features" = right-click menu commands (Who is this, Describe this
user) plus `CONFIG_FEATURES` (`ping_chat`, `ai_polls`, `ai_reactions`), plus
Channel limits / everything off / reset to defaults / done buttons.

Channel rules (`ChannelRulesView`): pick a target (All of MeowBot, a command, or a feature)
and a channel allowlist. Keys are `server_config.ALL` (`*`), `cmd:<qualified name>` and
`feat:<feature>`. A target's own rule wins over the ALL rule, no rule means every channel,
and threads match their parent channel (`server_config.channel_ids`). Blocked users get
told where it does work (`` `/ask` only works in #bots here ``).

- The command list is built live from `bot.pending_application_commands` (subcommands are
  keyed by qualified name, like `generate image`). **New commands show up in the menu automatically.**
  Bot-owner-only commands and the menu itself are in `UNBLOCKABLE_COMMANDS`. Add new owner-only commands there.
- Enforcement: the global `@bot.check` `server_allows_command` raises `CommandDisabledHere`, and
  `on_application_command_error` answers it ephemerally (and prints every other error like py-cord's
  default handler did, since defining it replaces the default).
- Features are checked where they happen: `ping_chat` in `on_message`, `ai_polls` in `create_poll`,
  `ai_reactions` in `add_reaction`. A new toggleable feature = a key in `CONFIG_FEATURES` +
  `SERVER_CONFIG.feature_enabled(guild_id, key, channel)` at the spot it runs (always pass the
  channel, or channel rules can't apply).
- Settings key on `ctx.guild_id`, so they also apply when someone uses the user-installed app in that server.

## Persistent memory

`memory_store.MemoryStore` (`memory.db`). Off by default. Owner toggles it with
`/persistent_memory`. When on: histories load from and save to the DB, chats are logged,
and the model gets `save_memory` / `forget_memory` tools and a block of saved facts.
`/reset` wipes the user's rows.

## Image / video generation

- `generate_image`: supporters (premium role, `PREMIUM_IMAGE_USES` per `PREMIUM_WINDOW`) get
  OpenRouter. Everyone else gets Cloudflare Workers AI (daily free allowance), then Pollinations as backup.
- `generate_video`: supporters only, OpenRouter (`PREMIUM_VIDEO_USES`).
- Images made or seen in a reply are kept per user (`remember_image`, `image_store`) so later
  turns can `view_image("img1")`.

## Environment variables (`.env`)

| Var | Use |
|---|---|
| `bot_token_dontleak` | Discord bot token (required) |
| `OLLAMA_HOST` | Ollama address |
| `OPENROUTER_API_KEY` | premium image/video gen |
| `workers_ai_key`, `CF_ACCOUNT_ID` | Cloudflare Workers AI free image gen |
| `AIHORDE_API_KEY` | AI Horde (optional, anonymous key otherwise) |
| `GITHUB_TOKEN` | higher GitHub API limits (optional) |
| `SEARXNG_URL` | self-hosted SearXNG, first in the search chain (read lazily via `searxng_url()`) |
| `DISABLE_DDG` | skip DuckDuckGo entirely (flagged IP) |
| `WEB_SEARCH_REGION` | DDG region, default `us-en` |
| `FIRECRAWL_API_KEY`, `TAVILY_API_KEY` | optional keys for those backends |
| `WHISPER_MODEL`, `WHISPER_DEVICE`, `VOICE_SPEECH_RMS` | voice listening tuning |
| `wavelink_password` | unfinished music feature |

## Conventions

- Code, comments, docstrings, commit messages and docs are clean and professional. Comments
  explain *why*, in plain lowercase-friendly prose, like the existing ones.
- Model-facing docstrings matter more than anything else in a tool. Treat edits to them as behavior changes.
- When writing Python that contains `\n` in strings, edit files directly (an editor or Edit tool),
  not through shell heredocs. Heredocs have mangled escapes here before.
- Use honest User-Agents (`MeowBot/1.0` for fetches). Don't rotate or spoof UAs or work around
  a service's access controls. If a keyless trick needs that, drop the service.
- Prefer keyless/free services. Anything paid or keyed must be optional and degrade gracefully.
- Branch is `master`. No commits or pushes unless the owner asks (pushing deploys, see above).

## Known issues / stale bits

- `README.md` is out of date (mentions llama 3.1 / gemma 3, old limits, few tools). Update it when touching user-facing features.
- Urban Dictionary's API returns 0 for every vote count now, so votes aren't shown.
- Wavelink/Lavalink music (`play_song`) is unfinished.

## Change log

Newest first. One line per change: date, what, where.

- 2026-11-16: `/public_server` channel rules: limit all of MeowBot, a command, or a feature to chosen channels (`ChannelRulesView`, `server_config` channel rules).
- 2026-11-16: Added `/public_server` (server owner setup menu), `server_config.py`, the global command check and `on_application_command_error`. `server_config.db` gitignored.
- 2026-11-16: "Describe this user" now sees badges, server tag, profile decorations, roles, boosting, permissions, devices and full activities (`profile_facts` in main.py). Prompt updated to use them.
- 2026-11-16: Added AGENTS.md (this file) and CLAUDE.md pointing to it.
- 2026-11-16: Tool registry (`Tool`, `TOOLS`, `TOOLS_BY_NAME` in main.py) replaced the `TOOL_SCHEMAS` lists and the `tool_label` / `run_tool` if-chains. `search_documents(query="")` default added.
- 2026-11-16: Custom emoji reactions can use any server's emojis except `EMOJI_BLOCKED_GUILDS`, and need the user's Use External Emojis permission.
- 2026-11-16: Polls and reactions check the asking user's permissions (`user_can`). Hardened the calculator (complex/inf results, comma-in-args bug, recursion) and capped lookup input lengths.
- 2026-11-16: New tools: calculator, currency_convert, dictionary (+urban), arxiv_search, lyrics (`extra_tools.py`), create_poll, add_reaction (`main.py`). System prompt tells the model to use calculator/currency_convert.
- 2026-11-16: Voice: `VoiceClient.stop()` no longer stops recording (barge-in fix). Direct decode path recovers from sequence resets.
