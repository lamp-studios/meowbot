"""Per-server settings that server owners change with /public_server.

Everything is ON everywhere unless a server says otherwise, so a server that never opened
the menu behaves exactly like before. A server can:

- turn commands and features off entirely, and
- limit commands and features to some channels ("channel rules"). A rule on one command or
  feature wins over the server-wide ALL rule, and no rule at all means every channel.
  Threads follow their parent channel's rule.

Rule keys are "cmd:<qualified name>" (like "cmd:ask" or "cmd:generate image"),
"feat:<feature>" (like "feat:ping_chat"), or ALL.

One row per server holds its settings as JSON, and every read comes from an in-memory
cache, because the global command check reads this on every single command.
"""
import json
import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_config.db")
ALL = "*"   # the channel rule key that covers every command and feature


def command_key(name):
    return f"cmd:{name}"


def feature_key(feature):
    return f"feat:{feature}"


def channel_ids(channel):
    """The ids a rule can match for this channel: itself, plus its parent for a thread."""
    if channel is None:
        return set()
    ids = {channel.id}
    parent = getattr(channel, "parent_id", None)
    if parent:
        ids.add(parent)
    return ids


class ServerConfig:
    def __init__(self, path=DB_PATH):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS servers (guild_id INTEGER PRIMARY KEY, settings TEXT NOT NULL)")
        self._db.commit()
        self._cache = {}
        for guild_id, raw in self._db.execute("SELECT guild_id, settings FROM servers"):
            try:
                self._cache[guild_id] = json.loads(raw)
            except ValueError:
                pass   # a corrupt row just means defaults for that server

    def _settings(self, guild_id):
        return self._cache.get(guild_id, {})

    def _update(self, guild_id, **changes):
        settings = {**self._settings(guild_id), **changes}
        settings = {k: v for k, v in settings.items() if v}   # empty = default, not stored
        with self._lock:
            if settings:
                self._cache[guild_id] = settings
                self._db.execute("INSERT OR REPLACE INTO servers (guild_id, settings) VALUES (?, ?)",
                                 (guild_id, json.dumps(settings)))
            else:   # back to all defaults, no row needed
                self._cache.pop(guild_id, None)
                self._db.execute("DELETE FROM servers WHERE guild_id = ?", (guild_id,))
            self._db.commit()

    # ---- turned off entirely ----

    def disabled_commands(self, guild_id):
        return set(self._settings(guild_id).get("disabled_commands", []))

    def set_disabled_commands(self, guild_id, names):
        self._update(guild_id, disabled_commands=sorted(names))

    def disabled_features(self, guild_id):
        return set(self._settings(guild_id).get("disabled_features", []))

    def set_disabled_features(self, guild_id, features):
        self._update(guild_id, disabled_features=sorted(features))

    # ---- channel rules ----

    def channel_rules(self, guild_id):
        """{rule key: [channel ids]} for every rule this server has."""
        return {key: list(ids) for key, ids in self._settings(guild_id).get("channels", {}).items()}

    def channel_rule(self, guild_id, key):
        return list(self._settings(guild_id).get("channels", {}).get(key, []))

    def set_channel_rule(self, guild_id, key, ids):
        """Limit `key` to these channels. No ids removes the rule (back to everywhere)."""
        rules = self.channel_rules(guild_id)
        if ids:
            rules[key] = sorted(set(ids))
        else:
            rules.pop(key, None)
        self._update(guild_id, channels=rules)

    def allowed_channels(self, guild_id, key):
        """The channel ids `key` is limited to here, or [] when it works in every channel."""
        rules = self._settings(guild_id).get("channels", {})
        return list(rules.get(key) or rules.get(ALL) or [])

    def _channel_ok(self, guild_id, key, channel):
        allowed = self.allowed_channels(guild_id, key)
        if not allowed or channel is None:   # no rule, or nothing to check against
            return True
        return bool(channel_ids(channel) & set(allowed))

    # ---- the checks the bot calls ----

    def command_enabled(self, guild_id, name, channel=None):
        if guild_id is None:
            return True   # DMs have no server settings
        return name not in self.disabled_commands(guild_id) and self._channel_ok(guild_id, command_key(name), channel)

    def feature_enabled(self, guild_id, feature, channel=None):
        if guild_id is None:
            return True
        return feature not in self.disabled_features(guild_id) and self._channel_ok(guild_id, feature_key(feature), channel)

    def reset(self, guild_id):
        """Everything on, everywhere."""
        with self._lock:
            self._cache.pop(guild_id, None)
            self._db.execute("DELETE FROM servers WHERE guild_id = ?", (guild_id,))
            self._db.commit()
