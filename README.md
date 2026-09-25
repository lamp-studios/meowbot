# Meow Bot
A silly AI assistant discord bot that likes soggy cat (***verryyy much*** for some reason) and based on llama 3.1 (gemma 3 for image recognition)!

it runs fully local with [ollama](https://ollama.com), so no API keys or paying for tokens, just your own PC/server doing the work :3

## what it can do
- **talk to you** with `/ask`, it remembers your last few messages so you can actually have a convo
- **look at images**, attach one to `/ask` and it switches to gemma 3 to see what's in it
- **read its replies out loud in VC** with `/ai_tts`, using [kokoro](https://github.com/thewh1teagle/kokoro-onnx) tts (also local)
- **server info**, shows member count, bot count and stuff
- works as a **server bot AND a user app**, so you can use it pretty much anywhere (except tts, that needs a server VC)

## commands
| command | what it does |
|---|---|
| `/ask <prompt> [image]` | ask the AI something, optionally with an image |
| `/reset` | wipes your conversation history (it forgets you exist) |
| `/ai_tts` | joins your VC and reads every new AI reply out loud |
| `/ai_tts_stop` | stops the tts and leaves the VC |
| `/server_info` | gets info on the current server |
| `/up` | owner only, syncs commands + reloads extensions |
| `/debugging` | owner only, shows tts status, queues and limits |

## limits (so nobody nukes the bot)
- prompts are capped at **700 characters**, anything longer gets rejected
- replies are capped at **800 tokens** and the AI gets **95 seconds** before it gives up
- chat history keeps your last **10 messages**, older ones get dropped
- tts reads up to **400 characters** per reply and only queues **5 lines** at once, extra ones get skipped

you can change all of these at the top of `main.py`

## what you need
- python 3.13 (uv grabs it for you if you don't have it)
- [uv](https://docs.astral.sh/uv/)
- [ollama](https://ollama.com) with `llama3.1:8b` and `gemma3:4b` pulled
- ffmpeg (for tts)
- the kokoro model files `kokoro-v1.0.onnx` and `voices-v1.0.bin` next to `main.py`, grab them from the [kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases)
- a `.env` file next to `main.py` with your bot token in it:
  ```
  bot_token_dontleak=your_token_here
  wavelink_password=your_lavalink_password
  ```
  (pls dont leak it) (`wavelink_password` is for music stuff that isn't finished yet, you can leave it empty for now)

## setup

### windows
just run `scripts/setup.bat`, it makes the venv, installs everything and asks if you wanna start the bot

### debian/ubuntu (24/7 hosting)
```bash
./scripts/setup.sh
```
this installs everything (ffmpeg, libopus, uv, python deps, ollama + the models) and asks if you want the bot to run 24/7. if you say yes it sets up a `meowbot` systemd service that starts on boot and restarts itself if it crashes

don't run it with sudo, it asks for your password when it needs to

useful stuff after that:
- `sudo systemctl status meowbot`, check if it's alive
- `journalctl -u meowbot -f`, see the logs live
- `sudo systemctl restart meowbot`, restart it after you change the code
- `sudo systemctl stop meowbot`, stop it
- `sudo systemctl disable meowbot`, stop it from starting on boot

### auto updates
the bot can pull new commits by itself and restart, so you just push and it updates :3
```bash
./scripts/install-autoupdate.sh        # checks every 5 min
./scripts/install-autoupdate.sh 1min   # or pick your own interval
```
it's careful about it:
- only fast-forward pulls, it never force resets or makes merge commits
- `.env`, the kokoro models and anything else gitignored/untracked never gets touched
- if you edited stuff on the server and the update would overwrite it, it skips the update instead
- reinstalls deps only if `pyproject.toml` or `uv.lock` changed
- if the new code doesn't even compile it rolls back and keeps the old bot running

logs: `journalctl -u meowbot-update -f`, update right now: `sudo systemctl start meowbot-update`

(you need a git remote with an upstream set for this, like `git push -u origin main`)

### manually
```bash
uv sync
uv run python main.py
```

## coming soon (maybe)
- music playing with lavalink/wavelink (the code is there, just commented out for now)
- random soggy cat pics from the [soggy API](https://soggyapi.sillycat.cloud) (for the AI to use)
