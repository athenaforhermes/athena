"""The engine supervisor: one pinokiod child process, and the truth about it.

Split out of `plugin_api.py` (rule 136). 561 lines that own a subprocess, a port, a log
and the readiness question -- the piece of Athena that most needs to be readable on its
own, and the piece that least needs to know about HTTP routes.

What it takes from its caller (the API module) rather than importing:

* `engine_dir`, `plugin_dir` -- where the engine and the browser shims live.
* `load_settings`, `save_settings` as callables -- because the settings *paths* stay in the
  API module, where three harnesses patch them (rule 153). A manager that imported the store
  directly would read and write a different file than the one the tests pointed at.

What it owns outright: the process, the port override, the read log, the readiness memo,
the `pids_from_*` parsers, and the 60-second listen memo that used to be defined four
thousand lines below its only reader.

Imported by name, because `plugin_api.py` is itself loaded by path (rule 136).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import credentials as athena_credentials
import node_runtime as athena_node

# The readiness memo. Process-wide, deliberately: it caches the answer to "is anything listening
# on our port", and a second manager asking the same question should get the memo, not a shadow of
# it. These two names used to sit four thousand lines below their only reader in `plugin_api.py`.
_PROBE_LOCK = threading.Lock()
_PROBE_AT: List[Any] = [0.0, False]


# ---------------------------------------------------------------- engine manager


def _pids_from_lsof(out: str) -> Set[str]:
    """`lsof -ti` prints one bare pid per line and nothing else."""
    return {line.strip() for line in out.splitlines() if line.strip().isdigit()}


def _pids_from_ss(out: str) -> Set[str]:
    """`ss -ltnpH` prints one line per socket, with `pid=1234` among the fields."""
    return set(re.findall(r"pid=(\d+)", out))


# ----------------------------------------------------- what a child process is given
# Nothing this module starts is ours. `boot.js` runs pinokio; the apps it runs are code
# cloned from arbitrary repositories; git runs whatever hooks a repository ships. So a
# child does not inherit the environment Hermes was launched with: provider keys, GitHub
# and HuggingFace tokens and anything else that landed in this process are not passed
# along. A child gets the keys named below and nothing else — settings, never secrets.
CHILD_ENV_KEYS = (
    # Windows: enough for a process to start, find its DLLs and know where it is.
    "SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT", "PATH",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "USERNAME",
    "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # POSIX: the same things under the names that platform uses.
    "USER", "LOGNAME", "SHELL", "TMPDIR", "DISPLAY",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    # Locale, encoding and clock. An app should see the same machine the person's own
    # shell does, or timestamps and filenames come out different than they look in it.
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "PYTHONIOENCODING", "PYTHONUTF8",
    # Reaching the network on terms the machine already has. A proxy or a CA bundle is a
    # setting, not a secret, and dropping it breaks every download behind a corporate
    # middlebox — which is the case where the person cannot see where it went wrong.
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO", "GIT_SSL_NO_VERIFY",
    # Which card and which model caches. These are a person's choices about their own
    # machine rather than credentials, and an app store that dropped them would run on
    # the wrong GPU, or re-download models that are already on the disk next to it.
    "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "NVIDIA_VISIBLE_DEVICES",
    "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE",
    "TORCH_HOME", "OLLAMA_HOST", "OLLAMA_MODELS",
    # Which store and profile this machine's Hermes serves: the coordinates a child of
    # ours (the `hermes` command line) has to agree with us about, and nothing past them.
    "HERMES_HOME", "HERMES_PROFILE",
)


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment a child process starts with: the keys above, and no others.

    Values come from this process's own environment, so a child inherits the *settings*
    of the machine — where its scratch space is, how to reach the network — and never the
    secrets that were loaded into the process it was started from. `extra` is for the
    handful of coordinates a particular child needs (its home, its port, a token the
    person has explicitly handed over); it is merged last and wins.
    """
    env = {name: os.environ[name] for name in CHILD_ENV_KEYS if name in os.environ}
    if extra:
        env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


# --------------------------------------------------------- the engine's own two tools
def _pinned_engine(engine_dir: Path) -> str:
    """The engine the manifest pins, as `name@version` — read, never guessed.

    The version is exact in `engine/package.json` (no range: an app store that
    fetched "8.x" would run a different engine on every machine), and every other
    package in the tree is pinned by the `package-lock.json` beside it.
    """
    try:
        body = json.loads((engine_dir / "package.json").read_text(encoding="utf-8"))
        deps = body.get("dependencies") or {}
        return ", ".join(f"{name}@{ver}" for name, ver in sorted(deps.items()))
    except Exception:  # noqa: BLE001 - no manifest is "we cannot say what it pins"
        return ""


def _npm_for(node: str) -> str:
    """npm as it ships beside a Node runtime — the one that matches that Node.

    Node's own directory first (the npm that came with the very interpreter the
    engine runs under), then this process's, then whatever is on PATH.
    """
    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    roots = [Path(p).parent for p in (node, sys.executable) if p]
    for root in roots:
        for name in names:
            if (root / name).is_file():
                return str(root / name)
    return shutil.which("npm") or ""


# How long a probe may wait before a port is called asleep.
# connections for a while before it answers HTTP, and every /status during that
# window used to block for the full engine timeout — measured 2.0s per call on a
# 5s UI clock while the whole boot was only a couple of seconds, which is what
# "Athena takes 10-30 seconds to start" was made of. 0.4s is long enough to tell
# an engine that is up from one that is still coming up, and short enough that a
# poll never makes the panel wait. Module level because it is a default argument
# of `EngineManager.listening`, evaluated when the class is built.
_STARTING_PROBE = 0.4

class EngineManager:
    """Owns the pinokiod child process: start, stop, readiness, proxy base."""

    def __init__(self, engine_dir: Path, plugin_dir: Path, load_settings, save_settings) -> None:
        # Injected, not imported: the settings paths are the API module's globals and three
        # harnesses redirect a run by patching them, so the manager asks its caller every time
        # rather than capturing a path of its own (rule 153).
        self.engine_dir = engine_dir
        self.plugin_dir = plugin_dir
        self._load_settings = load_settings
        self._save_settings = save_settings
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        # The engine's dependency install, when one is running: see `setup`.
        self._setup: Optional[threading.Thread] = None
        self._log: List[str] = []
        self._started_at: Optional[float] = None
        self._booting_since: Optional[float] = None
        # When this process first saw the engine coming up. Ours is measured from its
        # spawn time; an engine somebody else started is measured from this, because it
        # is the earliest moment this process can honestly claim to know about.
        self._last_error: str = ""
        # Set when the configured port is held by something that is not the
        # engine: this process then uses the fallback for the rest of its life.
        self._port_override: Optional[int] = None
        self._owner_at: float = 0.0
        self._owner_pid: Optional[int] = None
        # [when, is anything listening]. Only a NO is remembered, and only for a
        # couple of seconds: while the engine is down the panel polls every
        # second, and a netstat plus a connect that has to time out is most of a
        # second of a call whose whole answer is "still nothing".
        self._listen_at: List[Any] = [0.0, True]

    # -- helpers ---------------------------------------------------------

    def settings(self) -> Dict[str, Any]:
        return self._load_settings()

    @property
    def home(self) -> Path:
        return Path(self.settings()["home"])

    @property
    def port(self) -> int:
        return int(self._port_override or self.settings()["port"])

    @property
    def profile_dir(self) -> Path:
        return self.home / ".engine-profile"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _note(self, line: str) -> None:
        self._log.append(line)
        if len(self._log) > 400:
            del self._log[:200]

    # -- state -----------------------------------------------------------

    def is_running(self) -> bool:
        """True only for an engine THIS process spawned — see `port_owner()`."""
        return self._proc is not None and self._proc.poll() is None

    def listening(self, timeout: float = _STARTING_PROBE) -> bool:
        """Is anything at all holding our port? Decided in a fraction of a second.

        A TCP connect is the honest test, and this host is why it needs a budget:
        against a port with nothing behind it, Windows here DROPS the SYN instead
        of refusing it — measured 2.0s to fail against 127.0.0.1 and 4.0s against
        a name that resolves to ::1 first — so a probe of a dead port costs its
        whole timeout instead of failing instantly. A connect that has not landed
        within the budget is not an engine answering HTTP, so it is reported the
        same as nothing there; both callers handle that.
        """
        now = time.monotonic()
        if not self._listen_at[1] and now - self._listen_at[0] < 2.0:
            return False
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout)
        except OSError:
            s = None
        if s is None:
            self._listen_at = [now, False]
            return False
        s.close()
        self._listen_at = [now, True]
        return True

    def probe(self, timeout: float = 1.5, ttl: float = 1.0) -> bool:
        """True when the engine answers HTTP on its port.

        Cached for `ttl` seconds, single-flighted: every caller that needs to
        know whether the engine is up used to pay its own blocking round trip,
        so a busy engine stalled a whole set of concurrent polls. Pass ttl=0
        where a stale answer would change a decision — adopting a port, deciding
        to spawn — rather than just what a panel shows.
        """
        now = time.monotonic()
        with _PROBE_LOCK:
            if ttl and now - _PROBE_AT[0] < ttl:
                return _PROBE_AT[1]
        # A port with nothing behind it should be answered by a connect, not by an
        # HTTP round trip. On a host that drops the SYN of a dead port instead of
        # refusing it, `urlopen` spends the caller's whole timeout failing —
        # measured 1.5s on `/apps` and 2.0s inside every app health check while the
        # engine was stopped. `listening` remembers a NO for two seconds, which is
        # what makes a repeated poll free.
        if not self.listening(timeout=min(timeout, _STARTING_PROBE)):
            with _PROBE_LOCK:
                _PROBE_AT[0] = time.monotonic()
                _PROBE_AT[1] = False
            return False
        try:
            req = Request(self.base_url + "/", method="GET")
            with urlopen(req, timeout=timeout) as r:
                ok = 200 <= r.status < 500
        except HTTPError as e:
            ok = 200 <= e.code < 500
        except (URLError, OSError):
            ok = False
        with _PROBE_LOCK:
            _PROBE_AT[0] = time.monotonic()
            _PROBE_AT[1] = ok
        return ok

    def _port_pids(self) -> Set[str]:
        """PIDs listening on our port, whoever spawned them."""
        if sys.platform != "win32":
            # lsof is the portable answer where it exists and `ss` covers the hosts
            # that ship it instead. Neither being present is survivable: the engine
            # itself still stops by pid, and this only sweeps what a crash left
            # holding the port.
            for cmd, parse in ((["lsof", "-ti", f"tcp:{self.port}", "-sTCP:LISTEN"],
                                _pids_from_lsof),
                               (["ss", "-ltnpH", f"sport = :{self.port}"],
                                _pids_from_ss)):
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True,
                                         timeout=15).stdout
                except Exception:  # noqa: BLE001
                    continue
                pids = parse(out)
                if pids:
                    return {p for p in pids if p != str(os.getpid())}
            return set()
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except Exception:  # noqa: BLE001
            return set()
        pids: Set[str] = set()
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{self.port}"):
                pids.add(parts[-1])
        return {pid for pid in pids
                if pid.isdigit() and pid != str(os.getpid())}

    def _identify(self, timeout: float = 2.0) -> str:
        """What is answering on our port: `ours`, `foreign` or `none`.

        `probe()` only proves that SOMETHING speaks HTTP there, and adopting on
        that evidence alone would let Athena claim — and Stop would then kill — a
        service that merely happens to hold the port. An engine is the only
        thing that answers `/info/scripts` with a JSON object, so that is the
        test.
        """
        if not self.probe(timeout, ttl=0):
            return "none"
        try:
            req = Request(self.base_url + "/info/scripts", method="GET")
            with urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except (HTTPError, URLError, OSError, ValueError):
            return "foreign"
        try:
            return "ours" if isinstance(json.loads(raw), dict) else "foreign"
        except (ValueError, TypeError):
            return "foreign"

    def port_owner(self, ttl: float = 15.0) -> Optional[int]:
        """The PID serving our port, even one this process has no handle on.

        `_proc` only exists for an engine this process spawned. A backend
        restart — a Hermes update, or the app being quit and reopened — leaves
        the engine running with its parent gone and the port still held, while
        `_proc` is None and `is_running()` therefore reports stopped. Starting
        again then spawned a SECOND engine that died on the spot with
        `listen EADDRINUSE: address already in use :::42001` while the orphan
        carried on serving, which is what "Athena starts and then stops itself
        after ~10s" actually was. Ask this before ever spawning one.
        """
        # `_port_pids` shells out to netstat, so the answer is memoised briefly:
        # the owner of a port does not change often, and this is reachable from
        # `status()`, which the UI polls.
        now = time.time()
        if now - self._owner_at < ttl:
            return self._owner_pid
        pids = self._port_pids()
        pid = int(min(pids, key=lambda q: int(q))) if pids else None
        self._owner_at, self._owner_pid = now, pid
        return pid

    def status(self) -> Dict[str, Any]:
        running = self.is_running()
        if running:
            # Cheap path. A handle means the process is ours, so one probe
            # answers "ready yet" and nothing needs to shell out.
            # Short probe: with a handle the process is ours either way, so the
            # only question is whether it has started answering yet — and a poll
            # that waits the full engine timeout for that answer is 1.5s of a
            # panel that is trying to draw a boot.
            serving = self.probe(_STARTING_PROBE)
            state, adopted = ("ours" if serving else "starting"), False
        else:
            # No handle: something may still hold our port — an engine left by a
            # previous process, which is the normal case after any restart, and
            # the state every poll is in while one is booting. Ask who owns it
            # first (memoised: one netstat per 15s, not one per poll).
            if self.port_owner() is not None:
                # A held port that accepts connections but says nothing is an
                # engine still coming up, so a short probe settles it. Waiting
                # the full engine timeout here — on every poll — is what made a
                # 6s boot feel like half a minute. When it does answer, the same
                # short timeout is enough to tell ours from foreign.
                state = ("starting" if not self.probe(_STARTING_PROBE, ttl=0)
                         else self._identify(timeout=_STARTING_PROBE))
            elif self.listening():
                # Something took the port between the netstat and now, or the
                # netstat could not run. A held port that accepts but says nothing
                # is an engine coming up.
                state = ("starting" if not self.probe(_STARTING_PROBE, ttl=0)
                         else self._identify(timeout=_STARTING_PROBE))
            else:
                # Nothing is listening. On a host that drops the SYN of a dead
                # port instead of refusing it (see `listening`), asking the HTTP
                # layer anything here costs the whole timeout — 2.0s of every poll
                # while the engine is down. Netstat said nothing holds the port and
                # a connect landed nowhere: the engine is not there, and no probe
                # can say more than that.
                state = "none"
            serving = state == "ours"
            adopted = serving
        # How long the wait has been. "Athena takes a while to start" is a report nobody
        # can act on — the person waiting needs a number, and the panel is the only thing
        # in a position to count it. Measured from the spawn when the engine is ours, and
        # from the first poll that caught it coming up when it is not.
        if state == "starting":
            if self._booting_since is None:
                self._booting_since = time.time()
        else:
            self._booting_since = None
        since = self._started_at or self._booting_since
        return {
            # an engine we did not spawn is still a live engine: saying
            # "stopped" over a serving port is what made Start look like the
            # only way forward, and Start was what kept failing
            "running": running or adopted,
            "serving": serving,
            "ready": serving,
            "adopted": adopted,
            # `foreign` means another program holds the port — the UI can say so
            # instead of offering a Start that cannot succeed
            "engine_state": state,
            # A held-but-silent port is a boot in progress, and the panel says
            # "starting…" for it rather than "stopped". Its own field so nothing
            # has to infer it from three others.
            "starting": state == "starting",
            # Milliseconds spent waiting so far, or None when there is no honest start
            # to measure from (an engine that is simply up, or one nothing is waiting on).
            "starting_ms": (int((time.time() - since) * 1000)
                            if state == "starting" and since else None),
            "pid": (self._proc.pid if running and self._proc
                    else (self.port_owner() if state in ("ours", "starting") else None)),
            "port": self.port,
            "url": self.base_url,
            "home": str(self.home),
            "uptime": (time.time() - self._started_at) if (running and self._started_at) else None,
            "node": athena_node.find_node(),
            "engine_dir": str(self.engine_dir),
            "engine_present": (self.engine_dir / "node_modules" / "pinokiod" / "package.json").exists(),
            "last_error": self._last_error,
            "log": self._log[-40:],
        }

    # -- lifecycle -------------------------------------------------------

    def _identify_if_taken(self) -> str:
        """`_identify()` — but only when something is plausibly holding the port.

        Identifying a port with nothing behind it is not free on every host: this
        one drops the SYN instead of refusing it, so `_identify` waits out its
        timeout (measured 2.0s against 127.0.0.1, 4.0s where the name resolves to
        ::1 first) on every Start click and, before this, on every poll. Netstat
        and a short connect answer "is anything there at all" for a fraction of
        that; only if something is does the patient identification run.
        """
        if self.port_owner() is None and not self.listening():
            return "none"
        return self._identify()

    def start(self, wait: float = 60.0) -> Dict[str, Any]:
        with self._lock:
            if self.is_running():
                if self.probe():
                    return {"ok": True, "already": True, **self.status()}
                # process alive but not serving yet — let the caller wait
            elif (state := self._identify_if_taken()) == "ours":
                # An engine we did not spawn is already serving on our port.
                # Adopt it: spawning a second copy can only end in EADDRINUSE,
                # and the adopted one is already doing the job.
                self._owner_at = 0.0
                pid = self.port_owner()
                self._note(f"adopted running engine pid={pid} port={self.port}")
                return {"ok": True, "adopted": True, **self.status()}
            else:
                # A `foreign` port means something that is not an engine already
                # owns it, so a copy spawned here could only die with EADDRINUSE.
                # Pick a free port, then FALL THROUGH into the one spawn path
                # below. This must not be its own branch: the spawn lives inside
                # this `else`, so a separate arm skipped it entirely and start()
                # reported "engine exited early" for an engine it never launched.
                if state == "foreign":
                    free = athena_node.find_free_port(self.port + 1)
                    if free is None:
                        msg = (f"port {self.port} is held by another program and no "
                               f"free port near it was found")
                        self._last_error = msg
                        self._note(msg)
                        return {"ok": False, "error": msg, **self.status()}
                    self._note(f"port {self.port} is held by another program — "
                               f"using {free} instead")
                    cfg = self._load_settings()
                    cfg["port"] = free
                    try:
                        self._save_settings(cfg)
                    except Exception:  # noqa: BLE001
                        pass
                    # Authoritative for this process either way: ATHENA_PORT in
                    # the environment would otherwise override what was just
                    # saved and send the next start back into the conflict.
                    self._port_override = free
                node = athena_node.find_node()
                if not node:
                    self._last_error = (
                        "No compatible Node runtime found. Athena needs Node 22.x "
                        "(pinokiod's native addons have no prebuilds for Node 25/26)."
                    )
                    return {"ok": False, "error": self._last_error}
                if not (self.engine_dir / "node_modules" / "pinokiod").exists():
                    # The engine is provisioned, not shipped: the export that publishes
                    # this plugin leaves node_modules behind (native `.node` binaries are
                    # not allowed in the catalogue), so a fresh install has the engine's
                    # source and its lockfile and has to fetch its dependency tree once.
                    # Say so, and say which call does it — "Engine not installed" is what
                    # the catalogue reported with nothing to act on.
                    state = self.setup_state()
                    self._last_error = state["action"]
                    return {"ok": False, "error": self._last_error,
                            "setup": state}

                home = self.home
                profile = self.profile_dir
                home.mkdir(parents=True, exist_ok=True)
                profile.mkdir(parents=True, exist_ok=True)
                for sub in ("api", "bin", "cache", "logs"):
                    (home / sub).mkdir(parents=True, exist_ok=True)

                env = child_env()
                # Isolation: os.homedir() honors USERPROFILE on Windows, so the
                # kernel store lands under our profile dir, not the real ~/.pinokio.
                env["USERPROFILE"] = str(profile)
                env["HOME"] = str(profile)
                env["ATHENA_HOME"] = str(home)
                env["ATHENA_PORT"] = str(self.port)
                # Athena's own names go in; the engine's legacy ones are dropped so a
                # stale value from the user's shell cannot steer this child. (The
                # engine re-exports its own names into every app it runs — see
                # docs/NAMING.md for the mapping.)
                for _legacy in ("PINOKIO_HOME", "PINOKIO_SCRIPT_AUTOLAUNCH",
                                "PINOKIO_SCRIPT_REQUIRES"):
                    env.pop(_legacy, None)
                # An app must not open a web browser of its own. Gradio's
                # `inbrowser=True` — and any launcher that shells out to `start`
                # — puts the app's page in the user's default browser: outside
                # the window Athena draws for it, and outside the idle reaper's
                # sight (an app being typed into in that browser was reaped as
                # idle after 120s). Python's webbrowser runs $BROWSER in place of
                # a real browser, so this makes the call a no-op. Verified by
                # process count: the control run spawned a browser, this did not.
                for _name in (("no-browser.cmd",) if sys.platform == "win32"
                              else ("no-browser.sh", "no-browser.cmd")):
                    _no_browser = self.plugin_dir / "tools" / _name
                    if _no_browser.is_file():
                        env["BROWSER"] = str(_no_browser)
                        break
                # CUDA/torch tuning, applied to EVERY app at once. The engine's
                # env is the base of each app's env (the kernel builds the child
                # env from process.env), so one edit here covers all apps — where
                # Pinokio would need a per-app environment.json edit.
                #
                # expandable_segments is the one that matters: the default caching
                # allocator fragments over a long session and then stalls retrying
                # allocations, which reads as "generation randomly got slower".
                # Growable segments avoid that. LAZY module loading trims cold
                # start; it is the CUDA 12.2+ default, set explicitly so a future
                # downgrade cannot silently reintroduce eager loading.
                #
                # setdefault, not assignment: if you already export either of
                # these, or an app sets them in its own environment.json (which
                # is applied later and wins), your value is left alone.
                #
                # Deliberately NOT set: TORCH_ALLOW_TF32_CUBLAS_OVERRIDE and
                # NVIDIA_TF32_OVERRIDE. They only affect fp32 matmuls, and this
                # stack runs bf16/int8 with flash-attn — so they would change
                # numerics for no measurable speed. cudnn.benchmark has no env
                # var at all (it is a torch API call), so it cannot be set here.
                env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
                env.setdefault("CUDA_MODULE_LOADING", "LAZY")
                # Credentials are deliberately NOT placed in this environment. The engine
                # re-exports its env into every app it runs, and those apps are code
                # cloned from arbitrary repositories — so one line here hands a token to
                # all of them at once, whether or not anyone meant it to. An app gets a
                # credential only when the person gives that app one, through that app's
                # own affordance or the files under cache/HF_AUTH and connect/ that its
                # own code reads. Athena's own fetches (a repository, a model) carry the
                # token explicitly, to the one child that needs it.

                creation = 0
                if sys.platform == "win32":
                    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    creation |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

                try:
                    self._proc = subprocess.Popen(
                        [node, str(self.engine_dir / "boot.js")],
                        cwd=str(self.engine_dir),
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL,
                        creationflags=creation,
                        # POSIX has no process group flag; a session is what makes
                        # the reaper above able to kill the tree rather than the top
                        # of it.
                        start_new_session=(sys.platform != "win32"),
                    )
                except Exception as e:  # noqa: BLE001
                    self._last_error = f"Failed to spawn engine: {e}"
                    return {"ok": False, "error": self._last_error}

                self._started_at = time.time()
                self._last_error = ""
                self._note(f"spawned engine pid={self._proc.pid} port={self.port} home={home}")

            # wait for HTTP readiness
            deadline = time.time() + wait
            while time.time() < deadline:
                if not self.is_running():
                    self._last_error = f"Engine exited early (code {self._proc.returncode if self._proc else '?'})"
                    return {"ok": False, "error": self._last_error}
                # A short probe with a short nap between: readiness is seen
                # within ~0.6s instead of ~2s, and the caller's wait is spent on
                # the engine's own boot rather than on our polling granularity.
                if self.probe(timeout=_STARTING_PROBE):
                    self._note("engine ready")
                    return {"ok": True, **self.status()}
                time.sleep(0.2)

            self._last_error = f"Engine did not answer on {self.base_url} within {wait:.0f}s"
            return {"ok": False, "error": self._last_error}

    def stop(self, wait: float = 15.0) -> Dict[str, Any]:
        with self._lock:
            if not self.is_running():
                # No handle — but the port may still be held by an engine this
                # process did not start (adopted, or orphaned by a restart).
                # Returning "already stopped" over a serving port makes Stop a
                # button that does nothing.
                if self._port_pids():
                    self._reap_port()
                    self._note("stopped an engine we did not spawn (reaped the port)")
                    return {"ok": True, "reaped": True}
                return {"ok": True, "already": True}
            proc = self._proc
            assert proc is not None
            try:
                proc.terminate()
            except Exception:
                pass
            deadline = time.time() + wait
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
            else:
                try:
                    proc.kill()
                except Exception:
                    pass
            # the kernel forks helpers; make sure the port is actually free
            self._reap_port()
            self._proc = None
            self._started_at = None
            self._booting_since = None
            self._note("engine stopped")
            return {"ok": True}

    def _reap_port(self) -> None:
        """Best-effort: kill anything still listening on our port."""
        for pid in self._port_pids():
            try:
                if sys.platform == "win32":
                    # `/T` is the whole reason this works: without it taskkill takes the
                    # process that holds the port and leaves the app it spawned, which then
                    # keeps the port (and the VRAM) and answers the next start with "in use".
                    # On POSIX the group kill below is the same idea, and this has to be the
                    # same idea too -- a best-effort reap that reaps half is a leak with a log
                    # line.
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                   capture_output=True, timeout=15)
                else:
                    # The engine starts a session of its own (below), so the whole
                    # tree — the daemon and the children it forked — goes at once.
                    # Without the session, os.kill would take the parent and leave
                    # an app still holding the port.
                    try:
                        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError, ValueError):
                        os.kill(int(pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass

    # -- provisioning ----------------------------------------------------

    def _setup_consent(self) -> str:
        """The pin the person agreed to fetch, or "" — consent is per version, so a
        later release that pins a different engine asks again."""
        entry = self.settings().get("engine_setup")
        return str(entry.get("version") or "") if isinstance(entry, dict) else ""

    def setup_state(self) -> Dict[str, Any]:
        """What the engine needs to run, and what is missing — the consent prompt's facts.

        `engine/` ships source and a committed `package-lock.json`, not a
        `node_modules` tree: the catalogue refuses native `.node` binaries, so the
        dependency tree is built on the machine that will run it, once, from that
        lockfile. Nothing here touches the network; `provision` does, and only after
        the person has been told what it will fetch and from where.
        """
        pkg = _pinned_engine(self.engine_dir)
        lockfile = self.engine_dir / "package-lock.json"
        node = athena_node.find_node() or ""
        installed = (self.engine_dir / "node_modules" / "pinokiod").exists()
        return {
            "engine_dir": str(self.engine_dir),
            "pinned": pkg,
            "locked": lockfile.is_file(),
            "lockfile": lockfile.name if lockfile.is_file() else "",
            "node": node,
            "npm": _npm_for(node),
            "installed": installed,
            "installing": bool(self._setup is not None and self._setup.is_alive()),
            "consent": self._setup_consent() == pkg and bool(pkg),
            "registry": "npm's registry as this machine's npm is configured "
                        "(registry.npmjs.org by default)",
            "fetches": f"{pkg or 'the engine'} and its pinned tree, into "
                       f"{self.engine_dir}/node_modules",
            "action": "" if installed else
                      (f"the engine's dependencies are not at {self.engine_dir}: run the setup "
                       f"to fetch {pkg or 'the engine'} from npm, pinned by {lockfile.name}."),
        }

    def provision(self, consent: bool = False, timeout: float = 1800.0) -> Dict[str, Any]:
        """Fetch the engine's dependencies from the committed lockfile, once.

        Refused unless `consent` is true: this is the one call in the plugin that
        reaches npm and writes hundreds of packages into `engine/node_modules`, so the
        person is asked first and told what for — `setup_state` names the package, its
        exact version, the lockfile that pins every other one, and the registry.

        `npm ci` rather than `npm install`, deliberately: it can only lay down the
        locked tree (it refuses when `package.json` and the lockfile disagree), so what
        runs here is the engine the repository published rather than whatever a range
        resolved to today.
        """
        state = self.setup_state()
        if state["installed"]:
            return {"ok": True, "already": True, "state": state}
        if not consent:
            return {"ok": False, "needs_consent": True, "state": state,
                    "error": f"nothing is fetched until you agree: this downloads "
                             f"{state['pinned'] or 'the engine'} from npm into "
                             f"{state['engine_dir']}"}
        if not state["npm"]:
            return {"ok": False, "state": state,
                    "error": "npm was not found beside the Node runtime Athena uses — "
                             "install Node 22.x, then try the engine setup again"}
        cmd = [state["npm"], "ci", "--omit=dev", "--no-audit", "--no-fund"]
        # The npm cache goes in Athena's own home: this pulls a few hundred packages,
        # and the person's ~/.npm is neither where they expect an app store's downloads
        # nor guaranteed to have the room. Their ~/.npmrc still applies (same HOME).
        env = child_env({"npm_config_cache": str(self.home / "cache" / "npm"),
                         "npm_config_update_notifier": "false",
                         "npm_config_fund": "false"})
        self._note("engine setup: npm ci in " + str(self.engine_dir))
        try:
            p = subprocess.run(cmd, cwd=str(self.engine_dir), capture_output=True,
                               text=True, timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:  # noqa: BLE001
            self._note(f"engine setup failed to run npm: {e}")
            return {"ok": False, "error": f"could not run npm ci ({e})",
                    "state": self.setup_state()}
        out = (p.stdout or "") + (p.stderr or "")
        for line in out.strip().splitlines()[-8:]:
            self._note("  npm: " + line.strip()[:200])
        after = self.setup_state()
        if p.returncode == 0 and after["installed"]:
            cfg = self.settings()
            cfg["engine_setup"] = {"version": after["pinned"], "at": time.time(),
                                  "with": "npm ci", "where": str(self.engine_dir)}
            try:
                self._save_settings(cfg)
            except Exception:  # noqa: BLE001 - a saved consent is bookkeeping, not the install
                pass
            self._note(f"engine setup done: {after['pinned']}")
            return {"ok": True, "installed": True, "state": after}
        self._note("engine setup failed: npm ci exited " + str(p.returncode))
        return {"ok": False, "output": out[-4000:], "state": after,
                "error": "npm ci did not install the engine — the engine log has what "
                         "it said"}

    def setup(self, consent: bool = False) -> Dict[str, Any]:
        """The engine's dependencies: what they are, and — given consent — fetch them.

        With no consent this only answers, and starts nothing: it is what the panel
        asks before it can honestly put a download in front of anyone. With consent it
        runs `npm ci` from the committed lockfile in the background, because a few
        hundred packages take minutes and a request must not. `setup_state` then
        reports `installing` and the engine log carries each line npm printed; consent
        is recorded against the pin it was given for, so a second start after the same
        release does not ask again and a new pin asks afresh.
        """
        state = self.setup_state()
        if state["installed"]:
            return {"ok": True, "already": True, "state": state}
        if not consent:
            return {"ok": False, "needs_consent": True, "state": state}
        with self._lock:
            if self._setup is not None and self._setup.is_alive():
                return {"ok": True, "running": True, "state": state}
            self._setup = threading.Thread(target=self._setup_run,
                                           name="athena-engine-setup", daemon=True)
            self._setup.start()
        return {"ok": True, "running": True, "state": state}

    def _setup_run(self) -> None:
        """The background half of `setup()`: `npm ci`, once, into Athena's own tree."""
        try:
            out = self.provision(consent=True)
        except Exception as e:  # noqa: BLE001 - a failed setup is a log line, not a crash
            self._note(f"engine setup failed: {e}")
            return
        if not out.get("ok"):
            self._note("engine setup did not finish: " + str(out.get("error") or out))

    # -- raw engine call -------------------------------------------------

    def call(self, method: str, path: str, body: Any = None,
             query: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method)
        with urlopen(req, timeout=timeout) as r:
            raw = r.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"raw": raw.decode("utf-8", "replace")[:4000]}

# The singleton is built in `plugin_api.py`, which owns the settings callables it takes.


# --------------------------------------------- Hermes' own scheduling command line
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _cron_created(text: str) -> str:
    """The job id out of `Created job: <id>` — the one line that carries it."""
    m = re.search(r"Created job:\s*(\S+)", _ANSI_RE.sub("", text))
    return m.group(1) if m else ""


def _cron_failure(text: str) -> str:
    """Hermes' own sentence about why it refused, for the caller to show.

    Its refusals start with a known lead; anything else Hermes says (an argparse
    error, a crash) is still more use to the person than a message of ours invented
    to cover it, so the last line it printed is the fallback.
    """
    plain = _ANSI_RE.sub("", text).strip()
    for line in plain.splitlines():
        for lead in ("Failed to create job:", "Failed to remove job:"):
            if lead in line:
                return line.split(lead, 1)[1].strip()[:300]
    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def _cron_none(value: Optional[str]) -> Optional[str]:
    """The list command prints `None` for "has never run" and for "not scheduled"."""
    text = (value or "").strip()
    return None if text.lower() in ("", "none", "-", "never") else text


def hermes_cli() -> Optional[List[str]]:
    """How to run Hermes' own command line, or None when this machine has none.

    The interpreter hosting this plugin runs it — `hermes_cli` is importable from the
    Hermes install the dashboard belongs to — with `hermes` on PATH as the fallback.

    The child inherits this process's environment on purpose, and it is the one child
    that does. It is Hermes' own code, not code from a repository: its launcher reads
    the environment Hermes itself was started with, and a stripped one leaves that
    launcher half-finished and the command dead before it runs (measured: an env of
    PATH/SystemRoot/... alone fails inside `hermes_cli.main`). Nothing from an app runs
    in this child, and the job it creates runs later under Hermes' own scheduler.
    """
    if sys.executable:
        return [sys.executable, "-m", "hermes_cli.main"]
    found = shutil.which("hermes")
    return [found] if found else None


def ask_approval(tool: str, reason: str, rule_key: str = "") -> Dict[str, Any]:
    """Ask the person, through Hermes' own gate, whether to go ahead with this call.

    The same gate a dangerous shell command goes through, the same rule key the
    `athena_publish` tool already asks under (so an allow the person granted there —
    for this session or for good — answers this without asking twice), and the same
    failure direction: no gate, no approver, no answer means *not approved*. That last
    part is the point. A call that reaches here wanting a force-push is decided by a
    human, by their standing allow, or not at all; Hermes' own rules for cron and
    unattended contexts apply unchanged.

    Returns Hermes' result shape — ``{"approved": bool, "message": str}`` — so the
    caller can show Hermes' own words instead of inventing a refusal.
    """
    try:
        from tools.approval import request_tool_approval
    except Exception:  # noqa: BLE001 - not running inside Hermes: no gate, no approval
        return {"approved": False, "message": "no approval gate is available here"}
    try:
        out = request_tool_approval(tool, reason, rule_key=rule_key)
    except Exception as e:  # noqa: BLE001 - fail closed, but say what happened
        return {"approved": False, "message": f"the approval gate could not be asked ({e})"}
    out = out or {}
    return {"approved": bool(out.get("approved")), "message": str(out.get("message") or "")}


# ------------------------------------------------------------- app credentials
# A token is Athena's to hold and an app's to be *given*, one app at a time. What is
# remembered is the answer, as a name — `{app id: {"HF_TOKEN": true}}` — in Athena's
# own settings. The value itself is never written down, here or anywhere else: a
# granted token is an environment variable of a process launched for that app, and
# it dies with that process.
#
# The rule the review asked for, stated once: an app gets nothing until a person has
# said yes *for that app*. Not a machine-wide yes, not a yes for the last app that
# asked, and never a fallback that hands it over when the question could not be put.

# The settings key the record lives under: names, never values.
APP_GRANTS_KEY = "app_credentials"

# One answer covers both spellings, because they are one credential: a library reads
# `HF_TOKEN` or `HUGGING_FACE_HUB_TOKEN`, `git` and `gh` read `GH_TOKEN` or
# `GITHUB_TOKEN`. The pair travels together or not at all — handing over half of one
# is how an app ends up authenticated with a name nobody agreed to.
CREDENTIAL_PAIRS: Dict[str, Tuple[str, ...]] = {
    "HF_TOKEN": ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"),
    "GITHUB_TOKEN": ("GITHUB_TOKEN", "GH_TOKEN"),
}

CREDENTIAL_WANTS = {
    "HF_TOKEN": "your HuggingFace token so it can download models",
    "GITHUB_TOKEN": "your GitHub token so it can reach private repositories",
}


def credential_question(app_id: str, name: str) -> str:
    """The sentence a person is asked — it names the app and what the token buys."""
    wants = CREDENTIAL_WANTS.get(name, f"your {name}")
    return f"Athena can give {app_id} {wants}. Give it to this app?"


def app_grants(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, bool]]:
    """The record of who has been given what, read out of Athena's own settings.

    Names only: `{"comfyui": {"HF_TOKEN": true}}`. A record that cannot be read — a
    string where a mapping belongs, an older shape, a hand-edited file — reads as
    empty, and empty is the fail-closed reading of it: the worst that follows is
    being asked again, never being handed something nobody agreed to.
    """
    raw = (settings or {}).get(APP_GRANTS_KEY)
    out: Dict[str, Dict[str, bool]] = {}
    if isinstance(raw, dict):
        for app_id, names in raw.items():
            if isinstance(names, dict):
                out[str(app_id)] = {str(k): bool(v) for k, v in names.items()}
    return out


def app_credential_env(app_id: str, creds: Dict[str, str],
                       grants: Optional[Dict[str, Dict[str, bool]]] = None,
                       approve: Optional[Any] = None,
                       ) -> Tuple[Dict[str, str], Dict[str, Dict[str, bool]]]:
    """What *this* app may be handed, and the record of the answers that decided it.

    `creds` is every credential this machine holds, in the shape
    `credentials.credential_env` returns. `grants` is `app_grants(settings)`.
    `approve` is Hermes' own gate (`ask_approval`), or None where there is none.

    Three rules, in the order they matter:

    * An app nobody has answered for is asked once per credential that actually
      exists here — no question about a token this machine does not hold. A yes is
      remembered; so is a no. A no is an answer, and an answered question is not put
      again: a prompt that comes back every launch is a prompt people learn to click
      through, which is worse than not asking.
    * No gate is not a refusal and is not remembered as one — it is silence, and it
      means no token. A machine with no gate today must still be asked tomorrow, so
      nothing is written down for it.
    * Only a remembered yes puts a value in the returned environment, and only the
      names that app was asked about.

    Returns `(env, record)`. `env` is empty unless something was granted; `record`
    is the whole mapping, for the caller to save. Nothing here reads or writes a
    file, and nothing here logs a value.
    """
    record = {str(k): dict(v) for k, v in (grants or {}).items()}
    held = dict(record.get(app_id) or {})
    env: Dict[str, str] = {}
    for name, spellings in CREDENTIAL_PAIRS.items():
        value = next((str(creds[s]) for s in spellings if creds.get(s)), "")
        if not value:
            continue                      # nothing held: nothing to ask, nothing to give
        if name not in held:
            if approve is None:
                continue                  # no gate: no answer, and none remembered
            answer = approve("athena_launch", credential_question(app_id, name),
                             f"athena:app-credential:{app_id}:{name}")
            held[name] = bool((answer or {}).get("approved"))
        if held[name]:
            env.update({s: value for s in spellings})
    if held:                              # an app with no answers leaves no trace at all
        record[app_id] = held
    return env, record


def gate_available() -> bool:
    """Whether Hermes' approval gate can be reached from here at all.

    The same import `ask_approval` makes, asked separately so a caller can tell
    "the person said no" from "there was nobody to ask": `app_credential_env`
    remembers the first and deliberately forgets the second.
    """
    try:
        from tools.approval import request_tool_approval  # noqa: F401 - reachability is the question
    except Exception:  # noqa: BLE001 - not running inside Hermes: no gate
        return False
    return True


def credentials_for_app(app_id: str, engine: Any) -> Dict[str, str]:
    """The credentials *an app* may be handed, asking the person once for it.

    The whole hand-off, in one place so every caller asks the same question and
    reads the same record: `manager.settings()` for the record, Hermes' gate for the
    answer, `app_credential_env` for the decision. The manager is only used for its
    settings accessors, which is why this takes one rather than reaching for a
    global.

    Every way this can fail — no settings, no gate, a manager mid-relocation, a
    record somebody hand-edited — ends the same way: an empty environment. There is
    no branch here that hands a token over without an answer, which is the property
    the review asked for and the reason this is not a one-liner at the call site.
    """
    try:
        cfg = engine.settings()
        held = app_grants(cfg)
        env, record = app_credential_env(
            app_id, athena_credentials.credential_env(engine), held,
            ask_approval if gate_available() else None)
        if record != held:
            engine._save_settings({**cfg, APP_GRANTS_KEY: record})
        return env
    except Exception:  # noqa: BLE001 - a hand-off we cannot decide on is one we do not make
        return {}


def scheduling_blocked() -> str:
    """Why Athena must not arrange a repeat from here, or "" when it may.

    Two of Hermes' own questions, asked rather than assumed. `current_cron_execution()`
    — the function behind the plugin context's `ctx.current_cron_execution()`, in
    `cron/execution_identity.py` — says whether this call is itself a scheduled run; a
    run that schedules more runs is how one repeat becomes a thousand, so Hermes gates
    it on `cron.allow_agent_scheduling`, off by default. Inside a scheduled run, with
    that off, the honest answer is a refusal that names the setting.
    """
    try:
        from cron.execution_identity import current_cron_execution
        inside = current_cron_execution()
    except Exception:  # noqa: BLE001 - no cron package here means no scheduled run
        return ""
    if inside is None:
        return ""
    try:
        from hermes_cli.config import load_config_readonly
        allowed = bool(((load_config_readonly() or {}).get("cron") or {})
                       .get("allow_agent_scheduling"))
    except Exception:  # noqa: BLE001 - an unreadable setting is not permission
        allowed = False
    if allowed:
        return ""
    return ("this call is running as a scheduled job, and Hermes allows scheduling "
            "from inside one only with cron.allow_agent_scheduling: true (it is off)")


class HermesCron:
    """Hermes' cron, driven through `hermes cron` — the door its own docs name.

    This plugin used to import `cron.jobs` directly and walk `sys.path` looking for it:
    a plugin reaching into another package's private module and moving the path to do
    it. The command line answers the same four questions — what Hermes makes of a
    schedule phrase, what jobs exist, how to make one, how to drop one — with every
    guard Hermes applies to a typed `hermes cron create` applied here too (the schedule
    parser that will fire the job, the script sandbox under the Hermes home, the
    delivery rules), and without depending on module layout that is free to move.

    Method names and shapes are the ones the routes were written against
    (`parse_schedule`, `create_job`, `get_job`, `remove_job`), so the store stays
    swappable: the live suite injects a fake with those same four methods.
    """

    #: Athena's own scripts are the only files this adds to Hermes' scripts directory,
    #: and the prefix is how it cleans up after itself without touching a script the
    #: person put there.
    PREFIX = "athena-"

    def __init__(self, timeout: float = 180.0) -> None:
        self.timeout = timeout

    def scripts_dir(self) -> Path:
        """Where Hermes keeps the scripts its own cron jobs are allowed to run."""
        return athena_credentials.hermes_home() / "scripts"

    def _run(self, args: List[str], timeout: float = 0.0) -> subprocess.CompletedProcess:
        cli = hermes_cli()
        if cli is None:
            raise RuntimeError("this machine has no `hermes` command line to schedule with")
        return subprocess.run([*cli, "cron", *args], capture_output=True, text=True,
                              timeout=timeout or self.timeout, env=dict(os.environ))

    def _out(self, args: List[str], timeout: float = 0.0) -> str:
        p = self._run(args, timeout)
        return (p.stdout or "") + (p.stderr or "")

    def _jobs(self, text: str) -> Dict[str, Dict[str, str]]:
        """`hermes cron list --all` as {job id: {Name: ..., Next run: ..., Script: ...}}.

        The listing is a heading line per job (`  <id> [active|paused]`) with its
        detail lines indented under it. Parsing the person's own listing rather than a
        private store is the point: what the panel shows is what `hermes cron list`
        shows, because it is the same text.
        """
        jobs: Dict[str, Dict[str, str]] = {}
        here: Optional[Dict[str, str]] = None
        for raw in text.splitlines():
            line = _ANSI_RE.sub("", raw)
            head = re.match(r"^\s+(\S+)\s+\[(\w+)\]\s*$", line)
            if head:
                here = jobs.setdefault(head.group(1), {"state": head.group(2)})
                continue
            row = re.match(r"^\s+([A-Za-z][A-Za-z ]*):\s+(.*?)\s*$", line)
            if here is not None and row:
                here[row.group(1).strip()] = row.group(2)
        return jobs

    def _script_name(self, path: str) -> str:
        """Put Athena's script where `hermes cron create` will accept it, by name.

        `--script` has to be a bare filename under Hermes' own scripts directory: the
        sandbox refuses anything outside it, which is what the sandbox is for. So the
        file the route wrote is copied in under a name carrying our prefix, and
        `remove_job` deletes that copy when the job goes.
        """
        folder = self.scripts_dir()
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (self.PREFIX + Path(path).name)
        shutil.copyfile(path, dest)
        return dest.name

    def parse_schedule(self, schedule: str) -> Dict[str, Any]:
        """What Hermes' own parser makes of a phrase — by asking it, not by copying it.

        The public door to that parser is `cron create`, so this makes the cheapest job
        it will accept (our probe script, created paused so it is never armed and can
        never fire) and removes it again in the same breath. A phrase Hermes cannot read
        comes back as the refusal a person would get, as ValueError, which is what the
        route expects — the same parser that would fire the job is the one that read it.
        """
        probe = self.scripts_dir() / (self.PREFIX + "schedule-probe.py")
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("# Athena's schedule probe: written to ask Hermes' parser a\n"
                         "# question, created paused so it cannot fire, and removed with\n"
                         "# the job it was made for.\n", encoding="utf-8")
        jid = ""
        try:
            p = self._run(["create", str(schedule),
                           "--name", self.PREFIX + "schedule probe",
                           "--paused", "--no-agent", "--script", probe.name])
            text = (p.stdout or "") + (p.stderr or "")
            if p.returncode != 0:
                raise ValueError(_cron_failure(text) or "Hermes would not accept that schedule")
            jid = _cron_created(text)
            if not jid:
                raise ValueError("`hermes cron create` did not name the job it made")
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        made = (self.get_job(jid) or {}) if jid else {}
        if jid:
            self._run(["remove", jid])
        return {"kind": "schedule", "raw": str(schedule), "probe": jid,
                "display": made.get("schedule") or str(schedule)}

    def create_job(self, prompt: Any = None, schedule: str = "", name: Optional[str] = None,
                   script: Optional[str] = None, no_agent: bool = False,
                   interpreter: Optional[str] = None, workdir: Optional[str] = None,
                   **kw: Any) -> Dict[str, Any]:
        """Hand Hermes one no-agent job, and say what it made."""
        if prompt is not None or not no_agent or not script:
            raise ValueError("a no-agent job needs a script and no prompt")
        args = ["create", str(schedule)]
        if name:
            args += ["--name", str(name)]
        args += ["--no-agent", "--script", self._script_name(str(script))]
        if interpreter:
            args += ["--interpreter", str(interpreter)]
        if workdir:
            args += ["--workdir", str(workdir)]
        p = self._run(args)
        text = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            raise ValueError(_cron_failure(text) or "Hermes refused to create that job")
        jid = _cron_created(text)
        if not jid:
            raise ValueError("`hermes cron create` did not say which job it made")
        made = self.get_job(jid) or {}
        return {"id": jid, "name": name, "schedule": str(schedule), "script": str(script),
                "no_agent": True, "paused": False, "next_run": made.get("next_run"),
                "last_run": made.get("last_run")}

    def get_job(self, jid: str) -> Optional[Dict[str, Any]]:
        """One job as Hermes describes it, or None when it is not there."""
        rec = self._jobs(self._out(["list", "--all"])).get(str(jid))
        if not rec:
            return None
        return {"id": str(jid), "name": rec.get("Name"), "schedule": rec.get("Schedule"),
                "script": rec.get("Script"), "paused": rec.get("state") == "paused",
                "next_run": _cron_none(rec.get("Next run")),
                "last_run": _cron_none(rec.get("Last run"))}

    def remove_job(self, jid: str) -> bool:
        """Drop the job, and the copy of Athena's script that went in with it."""
        script = str((self.get_job(jid) or {}).get("script") or "")
        p = self._run(["remove", str(jid)])
        if p.returncode != 0:
            return False
        if script.startswith(self.PREFIX):
            try:
                (self.scripts_dir() / script).unlink()
            except OSError:
                pass
        return True
