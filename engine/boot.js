#!/usr/bin/env node
/**
 * Athena engine launcher.
 *
 * Boots the vendored `pinokiod` server as a plain Node process — no Electron.
 * Spawned by Athena's Python backend, which owns the lifecycle.
 *
 * Isolation contract (see docs/SPIKE-VERDICT.md):
 *   - the PARENT sets USERPROFILE to our private profile dir, so the kernel's
 *     own store (~/.pinokio/config.json) lands there instead of the real one
 *   - ATHENA_HOME is the app home (where api/, bin/, cache/ live)
 *   - ATHENA_PORT overrides pinokiod's hardcoded DEFAULT_PORT (42000)
 *   - ATHENA_BIND overrides the listen address (default 127.0.0.1: loopback)
 *
 * Config store: we hand the kernel a store object whose `.store` seeds
 * `{ home }`; on first run the kernel clones it into its own (isolated)
 * ~/.pinokio/config.json and persists settings there from then on. This keeps
 * the real Pinokio config untouched while still giving us a durable store.
 */

const path = require("path");
const fs = require("fs");

const ROOT = __dirname;

const os = require("os");

const HOME = process.env.ATHENA_HOME || path.resolve(ROOT, "..", "home");
const PORT = Number(process.env.ATHENA_PORT || 42001);

// The kernel keeps its own store at ~/.pinokio/config.json (inside the private
// profile dir its parent gave it) and, once written, `store.get("home")` wins
// over anything we pass in. So a home change would otherwise be ignored forever
// after the first boot. Pin it to ATHENA_HOME on every start.
const STORE_PATH = path.join(os.homedir(), ".pinokio", "config.json");

function pinHome() {
  try {
    fs.mkdirSync(path.dirname(STORE_PATH), { recursive: true });
    let cfg = {};
    try { cfg = JSON.parse(fs.readFileSync(STORE_PATH, "utf8")) || {}; } catch (_) {}
    if (cfg.home !== HOME) {
      cfg.home = HOME;
      fs.writeFileSync(STORE_PATH, JSON.stringify(cfg, null, 2));
      return true;
    }
  } catch (e) {
    emit({ event: "warn", stage: "pinHome", error: String(e && e.message ? e.message : e) });
  }
  return false;
}

const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "node_modules", "pinokiod", "package.json"), "utf8"));

function emit(obj) {
  process.stdout.write("[athena] " + JSON.stringify(obj) + "\n");
}

// The seed store handed to Kernel(); only `.store` matters (it is cloned).
const seed = { home: HOME };
const storeShim = {
  store: seed,
  get: (k) => (k in seed ? seed[k] : null),
  set: (k, v) => { seed[k] = v; },
  delete: (k) => { delete seed[k]; },
  exists: () => false,
  clone: () => {},
};

const repinned = pinHome();

emit({ event: "booting", home: HOME, port: PORT, pinokiod: pkg.version,
       node: process.version, rehome: repinned, store: STORE_PATH });

let Server;
try {
  Server = require(path.join(ROOT, "node_modules", "pinokiod", "server"));
} catch (e) {
  emit({ event: "fatal", stage: "require", error: String(e && e.stack ? e.stack : e) });
  process.exit(10);
}

const server = new Server({
  agent: "web",
  version: pkg.version,
  newsfeed: (r) => `https://pinokiocomputer.github.io/home/item?uri=${r}&display=feed`,
  profile: (r) => `https://pinokiocomputer.github.io/home/item?uri=${r}&display=profile`,
  site: "https://pinokio.co",
  discover_dark: "https://pinokio.co?embed=1&theme=dark",
  discover_light: "https://pinokio.co?embed=1&theme=light",
  portal: "https://pinokio.co",
  docs: "https://pinokio.co/docs",
  install: "https://pinokiocomputer.github.io/program.pinokio.computer/#/?id=install",
  store: storeShim,
});

// pinokiod hardcodes DEFAULT_PORT (server/index.js:165) and ignores config.port.
// start() only falls back to DEFAULT_PORT when this.port is falsy, so setting it
// here is the supported override — no patching of the vendored package.
server.port = PORT;

function shutdown(signal) {
  emit({ event: "shutdown", signal });
  try {
    if (server.kernel && typeof server.kernel.disposeVault === "function") {
      Promise.resolve(server.kernel.disposeVault()).catch(() => {});
    }
  } catch (_) {}
  setTimeout(() => process.exit(0), 300).unref();
  process.exit(0);
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

// Parent-liveness watchdog -------------------------------------------------
// The Python backend spawns this engine with subprocess.Popen and owns its
// lifetime, but its `_proc` handle is per-process state: after a Hermes restart
// (update, quit + reopen) we are orphaned and keep holding ATHENA_PORT with a
// dead parent, so every later start dies with `listen EADDRINUSE`.
//
// Killing ourselves on a bad guess is destructive, so bias hard toward staying
// alive: EPERM means the parent EXISTS but we may not signal it -> alive. Only a
// positive ESRCH counts as gone, and only after CONSECUTIVE_MISS_LIMIT misses.
const PARENT_PID = process.ppid;
const PARENT_POLL_MS = 5000;
const PARENT_MISS_LIMIT = 2; // ~10s of misses before we believe it

if (PARENT_PID && PARENT_PID > 1) {
  let misses = 0;
  setInterval(() => {
    // Never let this throw out of the timer: any unexpected error is treated as
    // "still alive" rather than taking the engine down.
    let gone = false;
    try {
      process.kill(PARENT_PID, 0);
    } catch (e) {
      gone = !!e && e.code === "ESRCH";
    }
    try {
      misses = gone ? misses + 1 : 0;
      if (misses >= PARENT_MISS_LIMIT) {
        emit({ event: "parent-gone", parent: PARENT_PID, misses });
        // stdout writes are async and process.exit() drops writes still queued in
        // the thread pool, so the line above would never reach the backend if we
        // exited here. Let it land first (exit code is still 0).
        setTimeout(() => process.exit(0), 100);
      }
    } catch (_) {}
  }, PARENT_POLL_MS);
}

process.on("uncaughtException", (e) => {
  emit({ event: "fatal", stage: "uncaught", error: String(e && e.stack ? e.stack : e) });
  process.exit(11);
});
process.on("unhandledRejection", (e) => {
  emit({ event: "fatal", stage: "unhandled", error: String(e && e.stack ? e.stack : e) });
  process.exit(12);
});

// Loopback by default. pinokiod's start() calls `listen(port)` with no host,
// which binds every interface — on a laptop that is the whole LAN, and this
// engine runs whatever an app's install scripts tell it to. Nothing outside
// this machine has any business reaching it, so a host is supplied whenever the
// caller gives none. The vendored package is left untouched: the shim lives in
// this process only, and only the engine's own listeners go through it.
const BIND_HOST = process.env.ATHENA_BIND || "127.0.0.1";
const http = require("http");
const _listen = http.Server.prototype.listen;
http.Server.prototype.listen = function (...args) {
  if (typeof args[0] === "number" && typeof args[1] !== "string") {
    return args[1] === undefined
      ? _listen.call(this, args[0], BIND_HOST)
      : _listen.call(this, args[0], BIND_HOST, args[1]);
  }
  if (args[0] && typeof args[0] === "object" && !args[0].host) {
    return _listen.call(this, { ...args[0], host: BIND_HOST }, args[1]);
  }
  return _listen.apply(this, args);
};

server
  .start({ debug: process.env.ATHENA_DEBUG === "1" })
  .then(() => {
    emit({
      event: "ready",
      port: server.port,
      home: HOME,
      homedir: server.kernel ? server.kernel.homedir : null,
      pid: process.pid,
    });
  })
  .catch((e) => {
    emit({ event: "fatal", stage: "start", error: String(e && e.stack ? e.stack : e) });
    process.exit(13);
  });
