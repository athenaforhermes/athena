"""Credential stores: what is on this machine, and sharing it with a guest.

Split out of `plugin_api.py`, which was 9,400 lines of routes interleaved with the
machinery behind them. The rule for this file is the same one the rest of `dashboard/`
follows: it is imported **by name**, because the API module is itself loaded by path
and a package-relative import there resolves at import time and then fails at call
time inside a plugin host.

One deliberate difference from the file it came out of: the engine's home is a
**parameter**, not a global. `plugin_api` owns the single `EngineManager` instance,
and a module that reaches for it would be a module that cannot be loaded, tested or
reasoned about without one being constructed first. Everything here that needs to
know where Athena lives is handed the engine, and the callers in `plugin_api` keep
their old zero-argument shapes through thin wrappers.

What this file deliberately does NOT do: it never writes a credential value into a
log, a response or an exception. It reports *names and places*, and copies files.

What it *does* write, and why — stated plainly, because the earlier version of this
docstring read as a stronger guarantee than the code made. Some apps read a
credential from a path and cannot be reached by an environment variable, so a
plaintext file form survives for them: owner-only permissions, inside a directory
the file browser refuses as a credential store, never logged, never sent off the
machine. `credential_env` is the form a launch should prefer, because an environment
variable leaves no copy behind. The files are the fallback for apps that cannot be
reached any other way; removing them outright would break those apps' sign-in, so
the two forms exist together rather than one replacing the other. What is still
missing is encryption at rest, which needs a keyring dependency this plugin does not
yet carry.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------- credentials


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / "AppData/Local/hermes"))


def athena_home(engine: Any) -> Path:
    """Athena's home — the engine's home, where apps, logs and environments live.

    One accessor rather than `engine.home` in eight places: the disk gate, the
    VRAM gate and the publisher all have to agree about where Athena is, and
    `engine.home` is a property that can move (a settings change, a different
    engine port) without any of those call sites noticing.
    """
    return Path(engine.home)


def hermes_env(engine: Any) -> Dict[str, str]:
    return dict(os.environ)


def _lock_down(path: Path, directory: bool = False) -> None:
    """Owner-only, best effort, on both kinds of filesystem.

    On POSIX the mode bits do the work. On Windows they do not: `os.chmod` only
    moves the read-only flag, so a file one account writes stays readable by
    every other account on the machine. The access control list is what actually
    decides, so it is rewritten to name the owner and nothing else — inheritance
    off, so the parent folder's grants do not re-open it.
    """
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r",
                                "/grant:r", f"{user}:F"],
                               capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass


def credential_env(engine: Any) -> Dict[str, str]:
    """The credentials an app should be handed at launch, as environment.

    The preferred mechanism. An environment variable lives for the life of the
    process and is written nowhere, so no plaintext copy outlives the app that
    needed it. The file forms in `seed_credentials` remain only because some
    apps read a path that no environment can reach; a launch that *can* use
    this should, and should not need the files at all.
    """
    out: Dict[str, str] = {}
    try:
        creds = collect_credentials(engine)
    except Exception:  # noqa: BLE001
        return out
    hf = creds.get("huggingface") or {}
    if hf.get("token"):
        # Both spellings: the libraries disagree about which one they read.
        out["HF_TOKEN"] = hf["token"]
        out["HUGGING_FACE_HUB_TOKEN"] = hf["token"]
    gh = creds.get("github") or {}
    if gh.get("token"):
        out["GITHUB_TOKEN"] = gh["token"]
        out["GH_TOKEN"] = gh["token"]
    return out


def pinokio_home() -> Optional[Path]:
    """Home of an existing Pinokio install, if the user has one."""
    try:
        cfg = Path.home() / ".pinokio" / "config.json"
        if cfg.exists():
            home = json.loads(cfg.read_text(encoding="utf-8")).get("home")
            if home and Path(home).exists():
                return Path(home)
    except Exception:
        pass
    return None


def pinokio_hf_tokens() -> List[Path]:
    """HuggingFace token files an existing Pinokio install already holds."""
    home = pinokio_home()
    if not home:
        return []
    return [
        home / "cache" / "HF_AUTH" / "token",
        home / "connect" / "huggingface" / "auth.json",
        home / "key.json",
    ]


def collect_credentials(engine: Any) -> Dict[str, Any]:
    """Credential sources Hermes already holds, so the user never re-enters them."""
    sources: Dict[str, Any] = {}

    # GitHub via gh CLI (keyring-backed)
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True,
                                 text=True, timeout=20)
            tok = (out.stdout or "").strip()
            if tok:
                sources["github"] = {"available": True, "source": "gh CLI", "token": tok,
                                     "masked": tok[:4] + "…" + tok[-4:]}
        except Exception:
            pass
    if "github" not in sources:
        sources["github"] = {"available": False, "source": "gh CLI (not logged in)"}

    # HuggingFace via the hf CLI in Hermes' venv, else a stored token file
    hf = shutil.which("hf") or shutil.which("huggingface-cli")
    hf_tok = ""
    if hf:
        try:
            out = subprocess.run([hf, "auth", "token"], capture_output=True,
                                 text=True, timeout=20)
            hf_tok = (out.stdout or "").strip().splitlines()[-1] if out.stdout else ""
        except Exception:
            hf_tok = ""
    if not hf_tok:
        for cand in (Path.home() / ".cache/huggingface/token",
                     Path.home() / ".huggingface/token"):
            if cand.exists():
                hf_tok = cand.read_text(encoding="utf-8").strip()
                break
    if not hf_tok:
        # Last resort: a HuggingFace token the user already gave an existing
        # Pinokio install. Reusing it is the whole point — no second login.
        for cand in pinokio_hf_tokens():
            try:
                raw = cand.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if not raw:
                continue
            if raw.startswith("{"):
                try:
                    hf_tok = json.loads(raw).get("access_token") or ""
                except Exception:
                    hf_tok = ""
            else:
                hf_tok = raw
            if hf_tok:
                break
    sources["huggingface"] = (
        {"available": True, "source": "hf CLI", "token": hf_tok,
         "masked": hf_tok[:4] + "…" + hf_tok[-4:] if len(hf_tok) > 8 else "…"}
        if hf_tok else {"available": False, "source": "hf CLI (no token)"}
    )

    # Model / provider keys from Hermes auth.json
    pool: Dict[str, Any] = {}
    try:
        auth = json.loads((hermes_home() / "auth.json").read_text(encoding="utf-8"))
        for prov, entries in (auth.get("credential_pool") or {}).items():
            if isinstance(entries, list) and entries:
                first = entries[0] if isinstance(entries[0], dict) else {}
                key = first.get("api_key") or first.get("access_token") or first.get("key")
                if key:
                    pool[prov] = {"available": True, "masked": str(key)[:4] + "…"}
    except Exception:
        pass
    sources["providers"] = pool
    return sources


def seed_credentials(engine: Any, target_home: Path) -> Dict[str, Any]:
    """Write Hermes-held credentials into the Athena home so apps inherit them."""
    creds = collect_credentials(engine)
    written: List[str] = []

    # HuggingFace — both formats pinokiod understands.
    hf = creds.get("huggingface") or {}
    if hf.get("available") and hf.get("token"):
        tok = hf["token"]
        hf_auth = target_home / "cache" / "HF_AUTH"
        hf_auth.mkdir(parents=True, exist_ok=True)
        (hf_auth / "token").write_text(tok, encoding="utf-8")
        _lock_down(hf_auth, directory=True)
        _lock_down(hf_auth / "token")
        written.append("cache/HF_AUTH/token")
        connect = target_home / "connect" / "huggingface"
        connect.mkdir(parents=True, exist_ok=True)
        (connect / "auth.json").write_text(json.dumps(
            {"access_token": tok, "token_type": "bearer", "source": "hermes"},
            indent=2), encoding="utf-8")
        _lock_down(connect, directory=True)
        _lock_down(connect / "auth.json")
        written.append("connect/huggingface/auth.json")

    # GitHub — git credential config so git clones authenticate as the user.
    gh = creds.get("github") or {}
    if gh.get("available") and gh.get("token"):
        gc = target_home / "gitconfig"
        text = gc.read_text(encoding="utf-8") if gc.exists() else ""
        if "[user]" not in text:
            text += "\n[user]\n  name = athena\n"
        if "longpaths" not in text:
            text = "[core]\n  longpaths = true\n" + text
        gc.write_text(text, encoding="utf-8")
        written.append("gitconfig")
        (target_home / "credentials" ).mkdir(parents=True, exist_ok=True)
        _lock_down(target_home / "credentials", directory=True)
        (target_home / "credentials" / "github.json").write_text(json.dumps(
            {"token": gh["token"], "source": "hermes-gh-cli"}, indent=2), encoding="utf-8")
        _lock_down(target_home / "credentials" / "github.json")
        written.append("credentials/github.json")

    return {"ok": True, "written": written,
            "github": bool(gh.get("available")),
            "huggingface": bool(hf.get("available")),
            "providers": sorted((creds.get("providers") or {}).keys())}


