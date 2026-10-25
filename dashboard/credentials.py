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

**One rule, and it is the whole file: a credential is read through Hermes, and a
credential is never written to a file.** The two names this plugin declares
(`HF_TOKEN`, `GITHUB_TOKEN` — `plugin.yaml`'s `requires_env`) are read with
`agent.secret_scope.get_secret(NAME)`, which honours the active profile's secret
scope and fails closed (`UnscopedSecretError`) when the process is multiplexing
with no scope installed. Failing closed is answered here as "this machine holds no
such credential" — `available: False` — never as a crash, and never as a fallback
read of some other store.

What this file deliberately does NOT do, because an earlier version did: it does
not shell out to `gh auth token` or `hf auth token`; it does not read
`~/.cache/huggingface/token` or `~/.huggingface/token`; it does not read an
existing Pinokio install's token files; and it does not read Hermes' `auth.json`.
Every one of those is another program's credential store, which a plugin may not
open (plugin-catalog admission rule 11 — credentials come through Hermes, never
from files).

It also does not write a token anywhere: no `cache/HF_AUTH/token`, no
`connect/huggingface/auth.json`, no `credentials/github.json`. A token reaches an
app as an environment variable for the life of the process that needs it
(`credential_env`), and is written nowhere — so no plaintext copy outlives the app,
survives in a backup or a folder sync, or stays readable after the app is gone.

What it *does* write is configuration, never a credential: `seed_credentials` keeps
`<home>/gitconfig` (long paths, a committer name) because a clone on Windows needs
it, and that file holds no secret. The rest reports *names and places*, never a
token value, in a log, a response or an exception.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# ----------------------------------------------------------- Hermes' own route


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / "AppData/Local/hermes"))


def _get_secret(name: str) -> Optional[str]:
    """Read a declared credential through Hermes' secret route, or None.

    THE only credential read in this file. `agent.secret_scope.get_secret` honours
    the active profile — under a multiplexed gateway another profile's key is not
    visible — and raises `UnscopedSecretError` when no scope is installed. Both
    "no scope active" and "not set" are the same answer to a caller here: this
    machine holds no credential to lend. Bare `os.environ` is never used as the
    fallback; `agent.secret_scope` owns that decision and makes it correctly
    (single-profile deployments inject credentials through the process env and the
    scope reads them there — this module does not second-guess that).

    `agent` is absent when this module is loaded outside the Hermes process (the
    live suite imports it under the plugin's own venv), which is the same answer
    again. The value is never logged and never written to a file.
    """
    try:
        from agent.secret_scope import get_secret  # type: ignore
    except Exception:  # noqa: BLE001 - not running inside Hermes
        return None
    try:
        val = get_secret(name)
    except Exception:  # noqa: BLE001 - incl. UnscopedSecretError: no credential, not a crash
        return None
    return val or None


def _masked(token: str) -> str:
    """A display form: enough to recognise, not enough to use. Never logged."""
    return (token[:4] + "…" + token[-4:]) if len(token) > 8 else "…"


def athena_home(engine: Any) -> Path:
    """Athena's home — the engine's home, where apps, logs and environments live.

    One accessor rather than `engine.home` in eight places: the disk gate, the
    VRAM gate and the publisher all have to agree about where Athena is, and
    `engine.home` is a property that can move (a settings change, a different
    engine port) without any of those call sites noticing.
    """
    return Path(engine.home)


def credential_env(engine: Any) -> Dict[str, str]:
    """The credentials an app is handed at launch, as environment.

    The only way a token reaches an app. An environment variable lives for the
    life of the process it is handed to and is written nowhere, so no plaintext
    copy outlives the app that needed it — which is exactly what the file forms
    this module used to write could not promise.
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
    """Home of an existing Pinokio install, if the user has one.

    A *path*, read from Pinokio's own `config.json` — that an install is present
    is a fact worth knowing when offering to build on it. No token of Pinokio's is
    read from anywhere: its credential store is not ours to open.
    """
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
    """Always empty.

    This used to name a Pinokio install's HuggingFace token files
    (`cache/HF_AUTH/token`, `connect/huggingface/auth.json`, `key.json`) so they
    could be read and reused. A token is no longer read from another program's
    store — it comes from Hermes' secret route (`_get_secret`). The name is kept
    because callers exist; the answer is "none".
    """
    return []


def collect_credentials(engine: Any) -> Dict[str, Any]:
    """The credentials Hermes holds for this profile, and where they come from.

    Two names, both declared in `plugin.yaml`'s `requires_env` and both read with
    `agent.secret_scope.get_secret`: `GITHUB_TOKEN` and `HF_TOKEN`. There is no
    other source — no `gh`/`hf` subprocess, no `~/.cache/huggingface/token`, no
    Pinokio token file, no direct read of Hermes' `auth.json`.

    `providers` stays an empty mapping on purpose: listing the model providers in
    Hermes' `auth.json` means reading Hermes' credential store directly, which is
    the read this module no longer makes.

    The shape is unchanged so existing callers keep working. `token` is what
    `credential_env` hands to an app; `masked` is a display form, and the
    `/credentials` route strips `token` before it answers.
    """
    del engine  # where Athena lives is not a credential source; the shape keeps it
    sources: Dict[str, Any] = {}

    gh = _get_secret("GITHUB_TOKEN") or ""
    sources["github"] = (
        {"available": True, "source": "Hermes (GITHUB_TOKEN)", "token": gh,
         "masked": _masked(gh)}
        if gh else {"available": False, "source": "Hermes (GITHUB_TOKEN not set)"})

    hf = _get_secret("HF_TOKEN") or ""
    sources["huggingface"] = (
        {"available": True, "source": "Hermes (HF_TOKEN)", "token": hf,
         "masked": _masked(hf)}
        if hf else {"available": False, "source": "Hermes (HF_TOKEN not set)"})

    sources["providers"] = {}
    return sources


def seed_credentials(engine: Any, target_home: Path) -> Dict[str, Any]:
    """Make an app's home usable — without writing a single token into it.

    This used to copy Hermes' HuggingFace and GitHub tokens into the Athena home
    as plaintext files (`cache/HF_AUTH/token`, `connect/huggingface/auth.json`,
    `credentials/github.json`) so that apps which read a path would inherit them.
    That write is gone and is not coming back: a token written to a path outlives
    the app that needed it, rides along in any backup or sync of that folder, and
    on Windows the file's permissions do not keep another account on the machine
    out. A token now reaches an app as an environment variable at launch
    (`credential_env`), for the life of the process, written nowhere.

    What is left is configuration, not credentials: `gitconfig` (long paths, a
    committer name), which holds no secret. `written` names only those files, so
    a caller can never report a token as seeded. Returns the same keys as before,
    so existing callers keep working unchanged.
    """
    written: List[str] = []
    try:
        gc = Path(target_home) / "gitconfig"
        text = gc.read_text(encoding="utf-8") if gc.exists() else ""
        if "[user]" not in text:
            text += "\n[user]\n  name = athena\n"
        if "longpaths" not in text:
            text = "[core]\n  longpaths = true\n" + text
        gc.write_text(text, encoding="utf-8")
        written.append("gitconfig")
    except Exception:  # noqa: BLE001 - a home that cannot be written is not a failure here
        pass

    try:
        creds = collect_credentials(engine)
    except Exception:  # noqa: BLE001
        creds = {}
    gh = creds.get("github") or {}
    hf = creds.get("huggingface") or {}
    return {"ok": True, "written": written,
            "github": bool(gh.get("available")),
            "huggingface": bool(hf.get("available")),
            "providers": sorted((creds.get("providers") or {}).keys())}


