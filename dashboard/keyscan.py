"""What an app will ask you for — read out of the app's own scripts.

The question this answers is the one people actually have before running
something off a store: *will this run on my box, or does it want an account and a
card?* Almost every AI app answers it in its own text — it reads
``process.env.OPENAI_API_KEY``, or it posts to ``https://api.replicate.com``.

Three rules keep this honest, because a badge that guesses is worse than no badge:

1. **Evidence or silence.** A key counts only when a script *names* it (an
   environment read, or a template like ``.env.example``), and a cloud call only
   when a literal host is written down. Documentation is skipped on purpose: a
   README naming ``OPENAI_API_KEY`` is a README talking, not the app asking.
2. **A capped scan says it was capped.** The walk stops at a file count and a byte
   budget so this can run on a real app folder without anyone watching it. Hitting
   either means "no key found" is a weaker statement, so ``local`` stays False and
   ``complete`` is what callers must check before making the strong claim.
3. **The names are never values.** A key name is not a secret; this module never
   reads or returns a value, only the name and the line it appeared on.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Iterator

# Folders that hold no app-authored text, or hold so much of it that reading it
# would turn a badge into a crawl. `env`/`venv` are the app's own runtime; a
# credential inside an installed package is the package's, not the app's.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".next", ".nuxt", ".svelte-kit", "dist", "build", "out",
    "env", "venv", ".venv", "site-packages", "conda-meta", "pkgs", "cache",
    "models", "checkpoints", "loras", "logs", "outputs", "bin", "Scripts",
    "Lib", "lib", ".cache", "vendor", "target", "coverage", ".tox",
}

# Only files that can *ask*. Docs are deliberately absent: see the module note.
SCRIPT_EXTS = {
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py", ".pyw", ".sh", ".bash",
    ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".go", ".rs", ".java",
    ".php", ".lua",
}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties"}
CONFIG_NAMES = {".env", ".env.example", ".env.sample", ".env.template", ".env.local",
                "env.example", "env.sample"}

MAX_FILES = 1500
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE = 6

# Reading an environment name, as the languages spell it. Each pattern captures the
# name in group 1 — one per syntax keeps the regexes readable and the failure mode
# obvious (a syntax nobody listed is a key nobody sees, so the list is long).
_REFS: Tuple[re.Pattern, ...] = (
    re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"process\.env\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
    re.compile(r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"Deno\.env\.get\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"os\.environ(?:\.get)?\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
    re.compile(r"os\.environ\.get\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"\bgetenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"\benviron\.get\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"),
    re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%"),
)

# `NAME=` at the start of a line in a config/template file: a key the app expects to
# find in the environment it is given. Restricted to credential-shaped names below,
# so `PORT=8000` does not turn into a finding.
_TEMPLATE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.M)

# An assignment whose right-hand side is *empty or a placeholder* is an ask, wherever
# it appears. One with a real value is not: an app that has a key written into its
# source is not asking for one, and reporting it would send someone hunting for a
# setting they do not need. Only the shape of the value is examined — never its text.
_ASSIGN_ASK = re.compile(
    r"^\s*(?:export\s+|const\s+|let\s+|var\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*"
    r"(?:['\"]\s*['\"]|['\"]?<?(?:YOUR|MY|INSERT|REPLACE|ADD|PUT|ENTER|TODO|XXX+|"
    r"CHANGEME|PLACEHOLDER)\b[^\n]*)$",
    re.M | re.I,
)

# A name is a credential when it *ends* in one of these words (the boundary is what
# keeps `MONKEY` out of it). `_ENABLED`/`_MODE`-style switches are excluded below
# rather than here, because they are the same word with a different ending.
_CRED_WORD = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTH|"
    r"ACCESS_KEY|CLIENT_SECRET|SESSION|SIGNATURE|CERT|SK)(?:_|$)"
)
# A name that ends this way is a *setting* about credentials, not a credential.
_NOT_CRED = re.compile(
    r"_(?:ENABLED|DISABLED|MODE|METHOD|TYPE|PROVIDER|HOST|PORT|NAME|REGION|TIMEOUT)$"
)

# Who the key belongs to. Only used to tell the user whether Hermes already holds
# one — a name with no owner still counts as a key, it just cannot be matched.
_OWNERS: Tuple[Tuple[str, str], ...] = (
    ("OPENAI", "openai"), ("ANTHROPIC", "anthropic"), ("CLAUDE", "anthropic"),
    ("HUGGINGFACE", "huggingface"), ("HUGGING_FACE", "huggingface"), ("HF_", "huggingface"),
    ("GEMINI", "google"), ("GOOGLE", "google"), ("REPLICATE", "replicate"),
    ("GROQ", "groq"), ("MISTRAL", "mistral"), ("COHERE", "cohere"),
    ("TOGETHER", "together"), ("OPENROUTER", "openrouter"), ("DEEPSEEK", "deepseek"),
    ("PERPLEXITY", "perplexity"), ("ELEVENLABS", "elevenlabs"), ("ELEVEN_LABS", "elevenlabs"),
    ("STABILITY", "stability"), ("RUNPOD", "runpod"), ("FAL_KEY", "fal"), ("FAL_API", "fal"),
    ("XAI", "xai"), ("GROK", "xai"), ("AZURE", "azure"), ("AWS", "aws"),
    ("CLOUDFLARE", "cloudflare"), ("R2_", "cloudflare"), ("DISCORD", "service"),
    ("TELEGRAM", "service"), ("SLACK", "service"), ("SMTP", "service"),
    ("MAILGUN", "service"), ("TWILIO", "service"), ("SENDGRID", "service"),
    ("PINECONE", "service"), ("MONGO", "service"), ("POSTGRES", "service"),
    ("MYSQL", "service"), ("REDIS", "service"), ("SUPABASE", "service"),
    ("FIREBASE", "service"), ("STRIPE", "service"), ("SUPABASE_", "service"),
)

# A hardcoded endpoint is a call-out even when no key name is anywhere, and it is
# the only other shape of evidence that a script reaches the network. `huggingface.co`
# is deliberately absent: it is where local weights are downloaded *from*, which is
# the local path, not a hosted API.
_CLOUD_HOSTS: Tuple[str, ...] = (
    "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com",
    "api.replicate.com", "api.elevenlabs.io", "api.stability.ai", "api.together.xyz",
    "openrouter.ai/api", "api.cohere.ai", "api.mistral.ai", "api.groq.com",
    "api.deepseek.com", "api.perplexity.ai", "api.x.ai", "runpod.io",
    "fal.run", "api.fal.ai", "api-inference.huggingface.co", "api.openai.azure.com",
)

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


def _owner(name: str) -> Optional[str]:
    for needle, prov in _OWNERS:
        if needle in name:
            return prov
    return None


def _is_credential(name: str) -> bool:
    if not ENV_NAME.match(name):
        return False
    if _NOT_CRED.search(name):
        return False
    return bool(_CRED_WORD.search(name))


def _candidates(folder: Path) -> Iterator[Path]:
    """Every file under `folder`, in a stable order, without enumerating it all first.

    `sorted(folder.rglob("*"))` used to be the first line of the walk, and sorting is what made
    it wrong: it forces the *whole* tree into memory and holds it before the file cap can apply,
    so `MAX_FILES = 1500` bounded the answer and not the work. A virtualenv or a model cache
    beside the app -- tens of thousands of files, which is the normal shape of these folders --
    was walked, stat'ed and sorted in full, and all but 1500 entries were then discarded. On a
    big app the scan simply sat there.

    This yields lazily instead: folders whose contents cannot name a key are pruned rather than
    filtered afterwards, each level is read in name order, and the caller stops it as soon as it
    has enough. Same list on the same tree, without the tree.
    """
    stack = [folder]
    while stack:
        here = stack.pop()
        try:
            entries = sorted(here.iterdir(), key=lambda e: e.name.lower())
        except OSError:
            continue
        deeper: List[Path] = []
        for e in entries:
            try:
                if e.is_dir():
                    if e.name not in SKIP_DIRS:
                        deeper.append(e)
                    continue
            except OSError:
                continue
            yield e
        # Reversed onto the stack so the deepest-first order is still name order.
        stack.extend(reversed(deeper))


def _files(folder: Path) -> Tuple[List[Path], bool, List[str]]:
    """The text worth reading, whether the walk was cut short, and what was skipped.

    An oversized *script* is the one skip that weakens the answer — a 900 KB script
    could name a key and we did not look — while an oversized config is a dataset
    bundled next to it (`data/train.json`), which cannot ask for anything. So the
    first makes the read incomplete and the second only gets reported.
    """
    out: List[Path] = []
    capped = False
    large: List[str] = []
    for p in _candidates(folder):
        if len(out) >= MAX_FILES:
            capped = True
            break
        try:
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.relative_to(folder).parts[:-1]):
                continue
            suffix = p.suffix.lower()
            if suffix not in SCRIPT_EXTS | CONFIG_EXTS and p.name.lower() not in CONFIG_NAMES:
                continue
            if p.stat().st_size > MAX_FILE_BYTES:
                if suffix in SCRIPT_EXTS:
                    large.append(p.relative_to(folder).as_posix())
                continue
        except OSError:
            continue
        out.append(p)
    return out, capped, large


def _read(path: Path, budget: List[int]) -> Optional[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > budget[0]:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    budget[0] -= len(text)
    return text


def scan(folder: Path, held: Optional[Iterable[str]] = None,
         folder_name: Optional[str] = None) -> Dict[str, Any]:
    """Read one app folder and say what it asks for.

    `held` is the set of providers Hermes already has a credential for, so a key
    can be reported as *answered* rather than *missing* — which is the difference
    between "install this and you are fine" and "install this and you need an
    account".
    """
    held_set: Set[str] = {str(h).lower() for h in (held or ())}
    folder = Path(folder)
    files, capped, large_scripts = _files(folder)
    budget = [MAX_TOTAL_BYTES]

    keys: Dict[str, Dict[str, Any]] = {}
    cloud: Dict[str, Dict[str, Any]] = {}
    env_seen: Set[str] = set()
    files_read = 0
    bytes_read = 0
    # A script nobody read is the only skip that makes "no key here" weaker.
    truncated = capped or bool(large_scripts)

    for path in files:
        text = _read(path, budget)
        if text is None:
            truncated = True
            continue
        files_read += 1
        bytes_read += len(text)
        try:
            rel = path.relative_to(folder).as_posix()
        except ValueError:
            rel = path.name
        template = (path.name.lower() in CONFIG_NAMES
                    or path.suffix.lower() in {".env"} or "env" in path.name.lower())

        names: List[Tuple[str, str]] = []
        for rx in _REFS:
            for m in rx.finditer(text):
                names.append((m.group(1), _line_of(text, m.start())))
        if template:
            for m in _TEMPLATE.finditer(text):
                names.append((m.group(1), _line_of(text, m.start())))
        else:
            # A script that leaves the key blank is asking; one that fills it in is not.
            for m in _ASSIGN_ASK.finditer(text):
                names.append((m.group(1), _line_of(text, m.start())))

        for name, line in names:
            if not ENV_NAME.match(name):
                continue
            if not _is_credential(name):
                env_seen.add(name)
                continue
            slot = keys.get(name)
            # The first line that names it is the evidence; later mentions are the
            # same fact, and a list of six mentions of one key is not more of an
            # answer than one.
            if slot is None:
                owner = _owner(name)
                keys[name] = {
                    "name": name,
                    "provider": owner or "other",
                    "held": bool(owner and owner in held_set),
                    "where": rel,
                    "line": _redact(line, name),
                }
            elif slot["where"] != rel:
                slot.setdefault("also", [])
                if len(slot["also"]) < 3 and rel not in slot["also"]:
                    slot["also"].append(rel)

        low = text.lower()
        for host in _CLOUD_HOSTS:
            if host in low:
                if host not in cloud:
                    cloud[host] = {"host": host, "where": rel,
                                   "line": _redact(_line_with(text, host))}

    # Answered first: a card that has room for two lines should spend them on the
    # keys that still need something from the user.
    key_rows = sorted(keys.values(), key=lambda k: (not k["held"], k["name"]))
    cloud_rows = sorted(cloud.values(), key=lambda c: c["host"])
    complete = not truncated
    return {
        "ok": True,
        "folder": folder_name or folder.name,
        # The strong claim needs the whole folder read: half a scan finding no key
        # is a fact about the scan, not about the app.
        "local": bool(complete and not key_rows and not cloud_rows),
        "complete": complete,
        "capped": capped,
        "skipped_large": [s for s in large_scripts][:5],
        "files_read": files_read,
        "bytes_read": bytes_read,
        "keys": key_rows[:MAX_EVIDENCE],
        "keys_total": len(key_rows),
        "held_count": sum(1 for k in key_rows if k["held"]),
        "cloud": cloud_rows[:MAX_EVIDENCE],
        "cloud_total": len(cloud_rows),
        "env_count": len(env_seen),
        "scanned_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _line_of(text: str, at: int) -> str:
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    return text[start:end if end > 0 else len(text)].strip()[:200]


# A name is not a secret, but the line a name sits on can hold one:
# `OPENAI_API_KEY = "sk-live-…"` is a real spelling in real scripts, and the
# evidence line is stored in a cache and returned by a route. So the value is
# taken out of the evidence — everything after the name's own `=` or `:`, and any
# long unbroken run anywhere on the line, which is what a token looks like.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-]{16,}")
_TOKEN_PREFIX = re.compile(r"\b(?:sk|pk|rk|hf|ghp|gho|ghs|xox[baprs]|AIza)[-_][A-Za-z0-9_\-]{4,}")


def _redact(line: str, name: str = "") -> str:
    """The evidence line with any value on it masked.

    A long run is masked unless it is spelled like an environment name, because
    `process.env["REPLICATE_API_TOKEN"]` is the evidence a reader came for and
    masking it would leave a line that proves nothing.
    """
    out = line
    if name:
        m = re.search(re.escape(name) + r"\s*[=:]\s*(\S.*)$", out)
        if m:
            out = out[:m.start(1)].rstrip() + "…"
    out = _TOKEN_PREFIX.sub("…", out)
    out = _TOKEN_RUN.sub(lambda m: m.group(0) if ENV_NAME.match(m.group(0)) else "…", out)
    return out[:200]


def _line_with(text: str, needle: str) -> str:
    low = text.lower()
    i = low.find(needle)
    return _line_of(text, i) if i >= 0 else ""


# --------------------------------------------------------------------- cache
#
# A scan is a directory walk, so it is stored rather than repeated: the card can
# then show the answer on every render without the store's grid paying for it.
# The cache lives in the plugin's own home, not in the app folder — an app's
# folder is the app's, and a file we drop in it is a file its update has to
# reckon with.

def cache_dir(home: Path) -> Path:
    return Path(home) / "cache" / "keyscan"


def cache_path(home: Path, folder_name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", folder_name or "app")
    return cache_dir(home) / (safe + ".json")


def load(home: Path, folder_name: str) -> Optional[Dict[str, Any]]:
    try:
        raw = cache_path(home, folder_name).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def save(home: Path, folder_name: str, result: Dict[str, Any]) -> None:
    p = cache_path(home, folder_name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, indent=1), encoding="utf-8")
    except OSError:
        pass


def scan_and_save(home: Path, folder: Path, held: Optional[Iterable[str]] = None,
                  folder_name: Optional[str] = None) -> Dict[str, Any]:
    res = scan(folder, held=held, folder_name=folder_name or folder.name)
    save(home, folder_name or folder.name, res)
    return res


def summarise(rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The cached scan as a card needs it: one line and a state, not the evidence."""
    if not rec:
        return {"scanned": False, "local": None, "keys": [], "keys_total": 0,
                "held_count": 0, "cloud_total": 0, "complete": False}
    return {
        "scanned": True,
        "local": rec.get("local"),
        "complete": bool(rec.get("complete")),
        "keys": [{"name": k.get("name"), "provider": k.get("provider"),
                  "held": bool(k.get("held"))} for k in (rec.get("keys") or [])],
        "keys_total": int(rec.get("keys_total") or 0),
        "held_count": int(rec.get("held_count") or 0),
        "cloud_total": int(rec.get("cloud_total") or 0),
        "scanned_at": rec.get("scanned_at"),
    }
