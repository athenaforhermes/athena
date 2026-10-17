"""Athena's runtime client — Athena's own front end onto the embedded engine.

The engine does not expose app control over HTTP. Its own UI drives everything
through a raw WebSocket RPC on the same port, so this module speaks that
protocol directly. That is what lets Athena own its interface: it asks the
engine to run things and reads the output back, without ever showing,
embedding, or re-skinning the upstream UI.

The contract below was established by reading the engine's socket server and
then verified live against pinokiod 8.2.0, including a real `install.js` run
that cloned and executed an app.

    resolve    {"uri": "~/api/<app>", "mode": "open", "id": "<app>"}
               -> {"data": {"uri": "<absolute path of the default script>"}}

    run        {"uri": "<absolute script path>", "id": "<same absolute path>"}
               -> {"id", "type": "start"|"stream"|"memory"|"error",
                   "data": {"raw": "..."}}, ending with data.type "shell.kill"
               The `uri` MUST name a concrete script file. Handing over the bare
               app directory sends the engine down a branch that calls a launcher
               method this build never defines, which crashes the process.

    stop       {"uri": "~/api/<app>[/<script>]", "id": "<the same value>", "stop": 1}
               The engine overwrites `uri` with `id`, so `id` is what has to be
               a `~/` path; a bare app name throws inside the kernel.

    status     {"uri": "~/api/<app>", "id": "<app>", "status": 1} -> {"data": bool}

    listen     {"uri": "~/api/<app>", "id": "<absolute script path>", "mode": "listen"}
               -> {"type": "connect", "data": {"state": <buffered output>}}
               `id` must match what the engine keys its buffer by, which is the
               resolved script path — not the app name.

Output is also written to disk under `<home>/logs/shell/`, which is what `logs()`
reads: it survives restarts and works even if nobody was listening.
"""

from __future__ import annotations

import json

import naming as athena_naming
import os
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from websockets.sync.client import connect as _ws_connect
except Exception:  # pragma: no cover - surfaced through available()
    _ws_connect = None

UA = "Athena/1.0"
# Packet shapes the engine has been observed to send. `shell.kill` is NOT in
# here: it closes one step's shell, and a script keeps going afterwards. The
# script is over at `event`/`stop`, which `run()` waits for.
KILL_TYPES = {"shell.kill", "shell.done", "api.done"}
LOG_LIMIT = 60000


_PORT_OPEN_AT: dict = {}


def _port_open(url: str, timeout: float = 0.35, ttl: float = 2.0) -> bool:
    """Is anything accepting TCP on this http URL's port? Answered cheaply.

    `urlopen` is the honest test of an engine, but not a cheap one on a host that
    DROPS the SYN of a port with nothing behind it rather than refusing it:
    measured 2.0s to fail against 127.0.0.1 here, 4.0s against a name that
    resolves to ::1 first, and the cost is the caller's whole timeout every time.
    A connect that has not landed inside a fraction of a second is not an engine
    answering HTTP, so that is the answer.

    A NO is remembered for `ttl` seconds (a YES never is, so a live engine is
    never missed), because while the engine is down its port is asked about by
    every health check and every poll.
    """
    now = time.monotonic()
    hit = _PORT_OPEN_AT.get(url)
    if hit and not hit[1] and now - hit[0] < ttl:
        return False
    try:
        # Everything, not just the connect: a caller may hand over a URL this
        # function cannot read (a port that was never resolved reads as
        # `127.0.0.1:None`, and `urlsplit` refuses it) — and this gate is called
        # *outside* the try that used to make a bad URL harmless, so a raise here
        # is a raise the caller never had to handle. An unreadable address is not
        # an engine answering, which is the only thing this function decides.
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname or "127.0.0.1", parts.port or 80
        s = socket.create_connection((host, port), timeout)
    except (OSError, ValueError, TypeError):
        _PORT_OPEN_AT[url] = (now, False)
        return False
    s.close()
    _PORT_OPEN_AT[url] = (now, True)
    return True


def _scrub(text: str) -> str:
    """Engine words become Athena's on the way to the screen.

    The engine's own log lines, errors and job steps still carry the vendor name.
    Renaming them here — at the boundary — is what lets the engine stay stock and
    stay updatable; a fork would have to be re-forked on every version bump.
    """
    return athena_naming.scrub_engine_text(text)


class RuntimeError_(Exception):
    pass


def available() -> bool:
    return _ws_connect is not None


def _now() -> float:
    import time
    return time.monotonic()


def _tail(text: str, limit: int = 20000) -> str:
    return text if len(text) <= limit else "…\n" + text[-limit:]


def _decode(raw):
    if isinstance(raw, (bytes, bytearray)):
        return {"data": {"raw": raw.decode("utf-8", "replace")}}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"data": {"raw": str(raw)}}


class Runtime:
    """One engine instance's control channel."""

    def __init__(self, port: int, home: str | os.PathLike | None = None,
                 host: str = "127.0.0.1"):
        self.host = host
        self.port = int(port)
        self.home = Path(home) if home else None

    # ------------------------------------------------------------- plumbing

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def http(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _require(self):
        if _ws_connect is None:
            raise RuntimeError_("the 'websockets' package is not available")

    def _conn(self, open_timeout: float = 20.0):
        return _ws_connect(self.ws_url, open_timeout=open_timeout,
                           close_timeout=3, max_size=8 * 1024 * 1024)

    def script_path(self, app: str, script: str | None = None) -> str:
        """Absolute path of a script inside an app folder."""
        if self.home is None:
            raise RuntimeError_("runtime has no home; needed to locate app scripts")
        base = self.home / "api" / app
        return str(base / script) if script else str(base)

    # ------------------------------------------------------------ operations

    def open_default(self, app: str, default: str | None = None,
                     timeout: float = 15.0) -> str | None:
        """The script the engine would run if you opened this app.

        A freshly cloned app resolves to its install script; an installed one
        resolves to whatever the app marks as its default action.
        """
        self._require()
        payload = {"uri": f"~/api/{app}", "mode": "open", "id": app}
        if default:
            payload["default"] = default
        with self._conn() as ws:
            ws.send(json.dumps(payload))
            for _ in range(4):
                try:
                    msg = _decode(ws.recv(timeout=timeout))
                except TimeoutError:
                    break
                except Exception:  # noqa: BLE001
                    break
                data = (msg or {}).get("data") or {}
                if isinstance(data, dict) and data.get("uri"):
                    return data["uri"]
        return None

    def open_default_or_first(self, app: str, menu, default: str | None = None,
                              timeout: float = 15.0) -> str | None:
        """The entry the app marks default, or its own first runnable choice.

        Most Pinokio apps mark a default; some (FaceFusion, Hunyuan3D) mark none
        and hide every runnable script inside a submenu. The engine cannot invent
        one, so ask the app what it offers and take the first entry that names a
        script — that is the install step on a fresh clone, the start action after.
        """
        try:
            script = self.open_default(app, default, timeout=timeout)
        except Exception:  # noqa: BLE001 - build the choices even with no engine
            script = None
        if script:
            return script
        try:
            m = menu(app) or {}
        except Exception:  # noqa: BLE001 - a fallback must never raise
            return None
        rows = []
        if isinstance(m, dict):
            rows = m.get("leaves") or m.get("entries") or m.get("items") or []
        for row in rows:
            href = row.get("href") if isinstance(row, dict) else None
            if isinstance(href, str) and href:
                return href
        return None

    def status(self, app: str, timeout: float = 10.0) -> bool:
        self._require()
        payload = {"uri": f"~/api/{app}", "id": app, "status": 1}
        with self._conn(open_timeout=timeout) as ws:
            ws.send(json.dumps(payload))
            for _ in range(3):
                try:
                    msg = _decode(ws.recv(timeout=timeout))
                except TimeoutError:
                    break
                except Exception:  # noqa: BLE001
                    break
                data = (msg or {}).get("data")
                if isinstance(data, bool):
                    return data
        return False

    def running(self, app: str | None = None, timeout: float = 15.0) -> dict:
        """What the engine is actually running, straight from its own book.

        `GET /info/scripts` is the only honest source for two things Athena
        needs: the **script path** that keys a running entry (which is what a
        stop must name), and the **URL** an app's web UI opened on. Neither can
        be guessed — the app folder is not the key, and the port is assigned by
        the app itself, not declared anywhere Athena can read ahead of time.

        Returns `{app_folder: [{"uri": <script path>, "url": <url or None>}]}`.
        """
        # Ask the cheap question first. On a host that drops the SYN of a port with
        # nothing behind it instead of refusing it — measured on Windows here: 2.0s
        # to fail against 127.0.0.1, 4.0s against a name that resolves to ::1 first
        # — an HTTP probe of a stopped engine costs its whole timeout, and this call
        # is on the path of every app health check. A connect that has not landed in
        # a fraction of a second is not an engine answering HTTP: skip the request.
        # A failed call already returned {} here, so nothing else changes.
        if not _port_open(self.http):
            return {}
        out: dict = {}
        try:
            req = urllib.request.Request(f"{self.http}/info/scripts",
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001 - engine down, or route absent
            return out
        if not isinstance(data, dict):
            return out
        for folder, entries in data.items():
            rows = []
            for e in (entries or []):
                if not isinstance(e, dict):
                    continue
                loc = e.get("local") if isinstance(e.get("local"), dict) else {}
                rows.append({"uri": e.get("uri") or "",
                             "url": (loc.get("url") or "").rstrip("/") or None})
            if rows:
                out[folder] = rows
        if app is None:
            return out
        want = app[:-4] if app.lower().endswith(".git") else app
        return {k: v for k, v in out.items()
                if k in (app, want) or (k.lower().endswith(".git") and k[:-4] == want)}

    def stop(self, app: str, script: str | None = None, timeout: float = 5.0) -> dict:
        """Stop whatever this app is running, by the key the engine filed it under.

        The engine keeps running scripts in `kernel.api.running`, keyed by the
        **resolved script path** — `/info/scripts` prints those keys. Stopping by
        an app-relative path misses: the entry for a launched app is filed under
        an absolute `.../<app>/start.js`, so `~/api/<app>` matched nothing and a
        running app survived its own stop. Query first, then stop each key.

        This deliberately does not wait for an answer. Stopping something that
        was never running is the common case (delete calls it before removing the
        folder), and the engine sends nothing back when there is nothing to stop,
        so waiting turned every delete of an idle app into a 20-second stall.
        """
        self._require()
        keys = []
        try:
            for rows in self.running(app).values():
                for r in rows:
                    if r.get("uri"):
                        keys.append(r["uri"])
        except Exception:  # noqa: BLE001
            keys = []
        if not keys:
            # Fall back to the app-relative form: it is what an app started
            # through the panel resolves to when the listing route is silent.
            ref = f"~/api/{app}/{script}" if script else f"~/api/{app}"
            keys = [ref]
        stopped = []
        for key in keys:
            payload = {"uri": key, "id": key, "stop": 1}
            try:
                with self._conn() as ws:
                    ws.send(json.dumps(payload))
                    # Drain briefly so a reply that does arrive is not cut off.
                    try:
                        ws.recv(timeout=0.4)
                    except Exception:  # noqa: BLE001 - silence is the normal answer
                        pass
                stopped.append(key)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "app": app, "error": str(e), "stopped": stopped}
        return {"ok": True, "app": app, "stopped_keys": stopped,
                "count": len(stopped)}

    def start(self, app: str, script: str | None = None, wait: float = 25.0,
              uri: str | None = None, input: dict | None = None) -> dict:
        """Kick off a script and return without waiting for it to finish.

        Scripts run inside the engine's own process tree; this socket is only a
        subscription to their output. Closing it does not stop the work, which is
        what lets a long install run while the panel stays responsive. Progress is
        read back through `logs()`.

        `uri` runs an exact script, skipping resolution — the caller passes one
        when it has already asked the app's menu which entry to run, together with
        the `input` that entry requires.
        """
        self._require()
        # Resolve the concrete script first — a bare app path is not runnable.
        if uri:
            resolved = uri
        elif script:
            resolved = self.script_path(app, script)
        else:
            resolved = self.open_default(app)
            if not resolved:
                # Not necessarily an error: an app whose menu offers only
                # submenus has no default at all, and the engine cannot invent
                # one. Say which it is so the caller can offer the choices.
                return {"ok": False, "app": app, "reason": "needs_choice",
                        "error": "this app has no default action — pick one to run"}
        uri = resolved
        with self._conn() as ws:
            payload = {"uri": uri, "id": uri}
            if input:
                payload["input"] = input
            ws.send(json.dumps(payload))
            deadline = _now() + wait
            while _now() < deadline:
                try:
                    msg = _decode(ws.recv(timeout=max(1.0, min(4.0, deadline - _now()))))
                except TimeoutError:
                    break
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "app": app, "uri": uri, "error": str(e)}
                if not isinstance(msg, dict):
                    continue
                data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
                if data.get("type") in KILL_TYPES:
                    return {"ok": True, "state": "finished", "app": app, "uri": uri}
                if msg.get("type") == "error":
                    return {"ok": False, "state": "error", "app": app, "uri": uri,
                            "error": _scrub(str(data.get("raw") or msg.get("data") or "")[:600])}
                if data.get("raw") or data.get("shell_id") or msg.get("type") in ("start", "stream"):
                    return {"ok": True, "state": "started", "app": app, "uri": uri}
        return {"ok": True, "state": "started", "app": app, "uri": uri}

    def run(self, app: str, script: str | None = None, uri: str | None = None,
            on_line=None, on_step=None, timeout: float = 7200.0,
            idle_timeout: float = 30.0, input: dict | None = None) -> dict:
        """Run a whole script to completion, streaming output as it arrives.

        `uri` runs an exact script path and skips resolution, which is what a
        caller that already asked the engine for the app's default action wants.

        `input` becomes the run's `args`: the engine stores it as
        `kernel.memory.args[id]` and templates read it back as `{{args.x}}`, so
        it is how a menu choice supplies the values a script requires — e.g.
        Hunyuan's `start.js` builds `--profile {{args.profile}}` and cannot run
        without one.

        Completion is the engine's `event`/`stop` packet, NOT the first
        `shell.kill`. A pinokio script is a list of steps and each step runs in
        its own shell, so a shell dying means one step finished and the next is
        starting. Treating the first `shell.kill` as the end of the run stopped
        every multi-step install after step one — a clone, with no build — and
        then closed the socket on the rest of the work.
        """
        self._require()

        target = uri
        if not target:
            if script:
                target = self.script_path(app, script)
            else:
                target = self.open_default(app)
                if not target:
                    return {"ok": False, "app": app, "error": "no runnable script"}

        lines: list[str] = []
        steps = 0

        def note(line: str) -> None:
            lines.append(line)
            if on_line:
                on_line(line)

        with self._conn() as ws:
            payload = {"uri": target, "id": target}
            if input:
                payload["input"] = input
            ws.send(json.dumps(payload))
            deadline = _now() + timeout
            while _now() < deadline:
                try:
                    msg = _decode(ws.recv(timeout=idle_timeout))
                except TimeoutError:
                    # A long download is legitimately silent for minutes.
                    return {"ok": True, "state": "running", "app": app, "uri": target,
                            "log": _tail("\n".join(lines)), "steps": steps,
                            "note": "still working — no output for a while"}
                except Exception as e:  # noqa: BLE001
                    return {"ok": bool(lines), "state": "disconnected", "app": app,
                            "uri": target, "log": _tail("\n".join(lines)),
                            "steps": steps, "error": str(e)}
                if not isinstance(msg, dict):
                    continue

                top = msg.get("type")

                # Failure arrives as a top-level `error` packet whose payload is
                # a plain string, not an object. Testing the payload as a dict
                # first silently dropped those: the loop then ran to its idle
                # timeout and reported a script that had died outright as merely
                # slow, which the panel showed as a success.
                if top == "error":
                    detail = msg.get("data")
                    if isinstance(detail, dict):
                        detail = detail.get("raw") or detail.get("message") or detail
                    return {"ok": False, "state": "error", "app": app, "uri": target,
                            "log": _tail("\n".join(lines)), "steps": steps,
                            "error": str(detail)[:600]}

                # The script is over. Either packet means the same thing.
                if top == "disconnect":
                    return {"ok": True, "state": "done", "app": app, "uri": target,
                            "log": _tail("\n".join(lines)), "steps": steps}
                if top == "event" and str(msg.get("data")).strip() == "stop":
                    return {"ok": True, "state": "done", "app": app, "uri": target,
                            "log": _tail("\n".join(lines)), "steps": steps}

                data = msg.get("data") if isinstance(msg.get("data"), dict) else {}

                # Per-step advance: the engine sends the step it is starting.
                if top == "start" and data.get("total"):
                    steps = int(data.get("current") or 0) + 1
                    total = int(data.get("total"))
                    if on_step:
                        on_step(steps, total, data.get("method") or "")

                if data.get("raw"):
                    for line in str(data["raw"]).splitlines():
                        note(_scrub(line))

                # `notify` is how a script talks to the person running it, and
                # pinokio apps use it for real instructions ("click the start
                # tab"). Dropping it loses the app's own final message.
                if top == "notify" and data.get("html"):
                    text = " ".join(str(data["html"]).split())
                    if text:
                        note(text)

                if data.get("type") == "error":
                    return {"ok": False, "state": "error", "app": app, "uri": target,
                            "log": _tail("\n".join(lines)), "steps": steps,
                            "error": _scrub(str(data.get("raw") or data)[:600])}

                # A shell ended — one step, not the script. Keep going.
                if data.get("type") == "shell.kill":
                    note(f"step {steps or '?'} finished")

        return {"ok": bool(lines), "state": "timeout", "app": app, "uri": target,
                "log": _tail("\n".join(lines)), "steps": steps}

    # -------------------------------------------------------- kernel methods

    def call_kernel(self, method: str, params: dict | None = None,
                    wait: float = 20.0, collect: float = 0.0) -> dict:
        """Invoke an engine kernel method and collect its output.

        This is the engine's internal API for provisioning its own toolchain
        (Miniforge, ffmpeg, and so on). Method paths are
        `kernel.bin.mod.<module>.<fn>` — the modules sit one level deeper than
        the namespace suggests, so `kernel.bin.conda.install` silently matches
        nothing while `kernel.bin.mod.conda.install` works.

        Like scripts, this work happens inside the engine: closing the socket
        does not cancel it, so provisioning continues after we stop listening.
        """
        self._require()
        lines: list[str] = []
        payload = {"method": method, "params": params or {}}
        try:
            with self._conn() as ws:
                ws.send(json.dumps(payload))
                first = _now() + wait
                stop_at = first + max(0.0, collect)
                while _now() < stop_at:
                    try:
                        msg = _decode(ws.recv(timeout=max(1.0, stop_at - _now())))
                    except TimeoutError:
                        break
                    except Exception:  # noqa: BLE001
                        break
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "error":
                        return {"ok": False, "error": _scrub(str(msg.get("data"))[:800])}
                    data = msg.get("data")
                    if isinstance(data, dict) and data.get("raw"):
                        lines.append(_scrub(str(data["raw"])))
                    if _now() >= first and not collect:
                        break
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True, "method": method, "log": _tail("".join(lines))}

    def environment(self) -> dict:
        """What the engine has provisioned for itself, and what an app needs."""
        facts: dict = {"home": str(self.home) if self.home else None}
        bin_dir = (self.home / "bin") if self.home else None
        facts["bin"] = str(bin_dir) if bin_dir else None
        # `conda_hook` is not a file on disk — the engine injects it into every
        # shell it spawns — so readiness is judged on the conda runtime itself.
        facts["conda"] = bool(bin_dir and (bin_dir / "miniforge").exists())
        try:
            url = f"{self.http}/pinokio/requirements_ready"
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                facts["engine"] = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            facts["engine"] = {"error": str(e)}
        facts["ready"] = bool(facts["conda"])
        return facts

    # ------------------------------------------------------------------ logs

    def logs(self, app: str, limit: int = LOG_LIMIT) -> str:
        """An app's accumulated output, read from the engine's own log files.

        Reading from disk rather than a live socket means the panel can show
        history for a script nobody was watching, and survives a restart.
        """
        if self.home is None:
            return "(no log: runtime has no home)"
        root = self.home / "logs"
        candidates: list[Path] = []
        for base in (root / "shell", root / "shell" / "cleaned"):
            d = base / "api" / app
            if d.is_dir():
                candidates.extend(sorted(d.rglob("*.txt")))
        if not candidates:
            d = self.home / "api" / app / "logs"
            if d.is_dir():
                candidates.extend(sorted(d.rglob("*.txt")))
        if not candidates:
            return f"(no output yet for {app})"

        parts: list[str] = []
        for path in candidates[-4:]:
            try:
                text = path.read_text("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            if not text.strip():
                continue
            parts.append(f"--- {path.name} ---\n{text}")
        if not parts:
            return f"(no output yet for {app})"
        blob = _scrub("\n".join(parts))
        return blob[-limit:] if len(blob) > limit else blob

    def port_for(self, app: str, timeout: float = 20.0) -> int | None:
        """The local port an app has opened, if any — the engine tracks these."""
        url = f"{self.http}/info/apps"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return None
        for a in (data.get("apps") or []):
            if (a.get("name") or a.get("id")) == app:
                return a.get("port")
        return None
