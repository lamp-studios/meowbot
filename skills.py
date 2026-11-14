"""Skills: instruction packs the chat model can load when a request needs them.

Each skill is a folder in skills/ holding a SKILL.md:

    ---
    name: discord-markdown
    description: One line saying what the skill is for. The model reads this to decide.
    triggers: table, code block, format
    ---
    The instructions themselves, in markdown.

How a skill reaches the model:
- the use_skill tool lists every skill's name and description, and loading one returns its
  instructions as the tool result
- if the user's message contains one of a skill's triggers (whole words, any case), the
  skill is added to the prompt up front. small models often forget to call a tool, so
  triggers are the reliable path for skills that should apply whenever a topic comes up

Skills are re-read whenever a SKILL.md changes, so adding or editing one needs no restart.
"""
import os
import re
import threading

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
MAX_SKILL_CHARS = 6000       # a skill is prompt, keep it from eating the context window
MAX_AUTO_SKILLS = 2          # triggered skills added to one prompt


class Skill:
    def __init__(self, name, description, triggers, body, path):
        self.name = name
        self.description = description
        self.triggers = triggers
        self.body = body
        self.path = path
        self._trigger_re = (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in triggers) + r")(?!\w)", re.I)
                            if triggers else None)

    def triggered_by(self, text):
        return bool(self._trigger_re and self._trigger_re.search(text or ""))


def _parse_frontmatter(raw):
    """Minimal frontmatter: 'key: value' lines between --- fences. Lists are comma separated
    or written as '- item' lines under the key."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    meta, current = {}, None
    for line in raw[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and current:
            meta.setdefault(current, [])
            if isinstance(meta[current], list):
                meta[current].append(item.group(1).strip().strip("\"'"))
            continue
        kv = re.match(r"^([\w-]+)\s*:\s*(.*)$", line)
        if kv:
            current = kv.group(1).lower()
            value = kv.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                value = value[1:-1]
            meta[current] = value.strip("\"'") if value else []
    body = raw[end + 4:].lstrip("\n")
    return meta, body


def _as_list(value):
    if isinstance(value, list):
        return [v for v in value if v]
    return [v.strip().strip("\"'") for v in str(value or "").split(",") if v.strip()]


class SkillLibrary:
    def __init__(self, folder):
        self.folder = folder
        self.skills = {}
        self._signature = None
        self._lock = threading.Lock()

    def _scan(self):
        if not os.path.isdir(self.folder):
            return []
        found = []
        for entry in sorted(os.listdir(self.folder)):
            path = os.path.join(self.folder, entry, "SKILL.md")
            if os.path.isfile(path):
                st = os.stat(path)
                found.append((path, st.st_mtime, st.st_size))
        return found

    def refresh(self):
        with self._lock:
            found = self._scan()
            if found == self._signature:
                return
            skills = {}
            for path, _, _ in found:
                try:
                    with open(path, encoding="utf-8") as f:
                        meta, body = _parse_frontmatter(f.read())
                except OSError as e:
                    print(f"[skills] couldn't read {path}: {e}")
                    continue
                name = str(meta.get("name") or os.path.basename(os.path.dirname(path))).strip().lower()
                description = str(meta.get("description") or "").strip()
                if not description:
                    print(f"[skills] {name} has no description, skipping it")
                    continue
                if len(body) > MAX_SKILL_CHARS:
                    print(f"[skills] {name} is over {MAX_SKILL_CHARS} chars, it gets cut off")
                    body = body[:MAX_SKILL_CHARS]
                skills[name] = Skill(name, description, _as_list(meta.get("triggers")), body.strip(), path)
            self.skills = skills
            self._signature = found
            print(f"[skills] loaded {len(skills)}: {', '.join(skills) or 'none'}")

    def get(self, name):
        self.refresh()
        key = (name or "").strip().lower().replace(" ", "-")
        return self.skills.get(key)

    def all(self):
        self.refresh()
        return list(self.skills.values())

    def triggered(self, text):
        self.refresh()
        return [s for s in self.skills.values() if s.triggered_by(text)][:MAX_AUTO_SKILLS]


SKILLS = SkillLibrary(SKILLS_DIR)


def skill_message(skill):
    return {"role": "system",
            "content": f"Skill loaded: {skill.name}. Follow these instructions for this reply.\n\n{skill.body}"}


def skill_context_for(prompt):
    """System messages for every skill the user's message triggers, like soggy_lore_for."""
    return [skill_message(s) for s in SKILLS.triggered(prompt)]


def use_skill_schema():
    """The use_skill tool, rebuilt each time so its description lists the current skills."""
    skills = SKILLS.all()
    if not skills:
        return None
    listing = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    return {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": ("Loads a skill: extra instructions for doing a specific kind of task well. "
                            "Call it before answering when the request matches one of these skills:\n" + listing),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "enum": [s.name for s in skills]}},
                "required": ["name"],
            },
        },
    }


def use_skill(name):
    skill = SKILLS.get(name)
    if not skill:
        names = ", ".join(s.name for s in SKILLS.all()) or "none"
        return f"no skill called {name!r}. available: {names}"
    return f"Skill {skill.name} loaded. Follow these instructions for this reply:\n\n{skill.body}"
