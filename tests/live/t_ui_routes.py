"""
Every route the desktop half calls must exist on the backend half.

The merged plugin.js builds URLs from templates (`/apps/${enc}/terminal`), the
backend declares them as router paths (`/apps/{app_id}/terminal`) -- and some are
declared once, parametrically (`POST /apps/{app_id}/{action}`), so install,
launch and stop all land on one line. Neither side can see the other, so a rename
on one side is a 404 nobody notices until a click.

Two shapes have to be resolved before comparing:

  * query strings   `/catalog?q=${encodeURIComponent(query)}` -> `/catalog`
  * sub-contexts    the ported file browser calls `ctx.rest('/read')` through
                    `subCtx(ctx, '/files', 'files')`, so it means `/files/read`.
                    Every `subCtx(...)` prefix in the merged file is a candidate
                    base for a relative URL, and any of them may be the one.

It also greps the merged file for the names the UI must not use any more, so a
half-finished rename fails here rather than in front of the user.

Usage: env -u PYTHONPATH <venv python> t_ui_routes.py
"""
import re
import sys
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")
UI = PLUG / "desktop" / "plugin.js"
API = PLUG / "dashboard" / "plugin_api.py"

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}   {detail}")


def norm(p):
    """`/apps/${x}/y?q=1` and `/apps/{app_id}/y` both become `/apps/{}/y`."""
    p = p.split("?")[0].split("#")[0]
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}]*\}", "{}", p)
    p = re.sub(r"/+$", "", p) or "/"
    return p


def segs(p):
    return [x for x in p.split("/") if x != ""]


def matches(ui, declared):
    """Same shape, with a declared `{}` standing for any single segment."""
    a, b = segs(ui), segs(declared)
    if len(a) != len(b):
        return False
    return all(x == "{}" or x == y for x, y in zip(b, a))


ui_src = UI.read_text(encoding="utf-8")
api_src = API.read_text(encoding="utf-8")

# URLs the UI hands to ctx.rest. Single quotes or backticks, and the call may sit
# mid-line, so match the literal rather than the whole line.
ui_urls = set()
def joined(src, end):
    """Whether the literal that ends at `end` is concatenated with something.

    `ctx.rest('/schedules/' + encodeURIComponent(id), ...)` is one segment short
    of the route it means, and the missing piece is a path parameter. Standing one
    in is the difference between reading the call and misreading it as the
    collection route — with the verb check below, that misreading was a DELETE
    against `/schedules`, which nothing serves.
    """
    return re.match(r"\s*\+", src[end:]) is not None


for m in re.finditer(r"ctx\.rest\(\s*[`'\"]([^`'\"]+)[`'\"]", ui_src):
    u = m.group(1)
    if u.startswith("/"):
        ui_urls.add(norm(u.rstrip("/") + "/{}" if joined(ui_src, m.end()) else u))

# Bases the ported panels address their URLs through.
bases = [""] + sorted(set(re.findall(r"subCtx\([^,]+,\s*'([^']+)'", ui_src)))

def routes_in(src, prefix=""):
    """Every path this module's router declares, with its mount prefix applied."""
    out = set()
    for m in re.finditer(r'@router\.(?:get|post|put|delete|patch)\(\s*"([^"]+)"', src):
        out.add(norm(prefix + m.group(1)))
    return out


def routes_with_verbs(src, prefix=""):
    """{(path, METHOD)} — because a path is only half of a route.

    `/files/trash` served as GET and called as POST is a 405 the panel shows as a
    generic failure, and nothing in the check above can see it: the path matches.
    """
    out = set()
    for m in re.finditer(r'@router\.(get|post|put|delete|patch)\(\s*"([^"]+)"', src):
        out.add((norm(prefix + m.group(2)), m.group(1).upper()))
    return out


def call_args(src, open_paren):
    """The arguments of a call, by counting depth and respecting quotes.

    A window of characters will not do: the next `ctx.rest(` sits inside the
    options object of this one, so a window long enough to reach a `method:` here
    is long enough to read the method that belongs to the call after it.
    """
    depth, i, quote = 0, open_paren, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return src[open_paren + 1:i]
        i += 1
    return src[open_paren + 1:]


def calls_with_verbs(src):
    """Every ctx.rest call site, as (url, METHOD or None).

    Returns (calls, sites, unread) where `calls` is the set of distinct
    (path, verb) pairs worth comparing, `sites` is how many `ctx.rest(` calls the
    file contains at all, and `unread` is where the path was not a literal — a
    helper forwarding its own argument. Those five-odd lines are listed rather
    than counted, because "some were skipped" is exactly what a check that reads
    source must not say quietly.

    Method is None where the call does not write one down: those are compared by
    path only, since guessing GET for a POST is how a check grows a false failure.
    """
    out, sites, unread = set(), 0, []
    for m in re.finditer(r"ctx\.rest\(", src):
        sites += 1
        args = call_args(src, m.end() - 1)
        lit = re.match(r"\s*[`'\"]([^`'\"]+)[`'\"]", args)
        if not lit or not lit.group(1).startswith("/"):
            unread.append(src[:m.start()].count("\n") + 1)
            continue
        mm = re.search(r"method:\s*['\"]([A-Za-z]+)['\"]", args)
        url = lit.group(1)
        if joined(src, m.end() + lit.end()):   # indices in `args` are src indices + (m.end())
            url = url.rstrip("/") + "/{}"     # see `joined` above
        out.add((norm(url), mm.group(1).upper() if mm else None))
    return out, sites, unread


def mounted_prefixes(src):
    """{module file stem: mount prefix} for `include_router(<alias>.router, prefix=..)`.

    The file browser and the machine monitor are routers of their own, mounted
    under /files and /sys, so a `/read` in the UI is really `/files/read`.
    """
    # `import files as athena_files` maps the alias to the module file.
    alias = {a: m for m, a in re.findall(r"import\s+(\w+)\s+as\s+(\w+)", src)}
    out = {}
    for m in re.finditer(r"include_router\(\s*(\w+)\.router,(?:\s|\n)*prefix=\"([^\"]+)\"", src):
        out[alias.get(m.group(1), m.group(1)) + ".py"] = m.group(2)
    return out


DASH = PLUG / "dashboard"
api_routes = routes_in(api_src)
api_verbs = routes_with_verbs(api_src)
mounts = mounted_prefixes(api_src)
for fname, prefix in mounts.items():
    f = DASH / fname
    if f.exists():
        body = f.read_text(encoding="utf-8")
        api_routes |= routes_in(body, prefix)
        api_verbs |= routes_with_verbs(body, prefix)
print(f"  note mounted routers: {mounts}")

print(f"UI calls {len(ui_urls)} distinct URLs  (sub-context bases: {bases})")
print(f"backend declares {len(api_routes)}")

missing = sorted(u for u in ui_urls
                 if not any(matches(b + u, d) for b in bases for d in api_routes))
check("every route the UI calls exists on the backend", not missing, ", ".join(missing))

# --- the verb, not only the path ---------------------------------------------
# Half a route is the path. The other half is the method, and the two halves are
# written in different files: `/files/trash` is a POST and asking for it with GET
# is a 405 the panel renders as a failure with no reason in it. Comparable only
# where the call writes its method down; where it does not, the path check above
# is all there is, and the count of those is printed rather than passed over.
ui_calls, sites, unread = calls_with_verbs(ui_src)
guessed = sorted(u for u, m in ui_calls if m is None)
written = sorted((u, m) for u, m in ui_calls if m is not None)


def answered_with(u, method):
    """Whether some mount base and declared route answers this call with this verb.

    Segment-wise, by the same `matches()` the path check uses: the UI writes
    `/apps/${enc}/install` and the backend declares one parametric
    `/apps/{app_id}/{action}` that answers install, launch and stop alike.
    """
    return any(mm == method and matches(b + u, p)
               for p, mm in api_verbs for b in bases)


verb_bad = [f"{m} {u}" for u, m in written if not answered_with(u, m)]
check(f"every call that names its verb finds that verb on that path ({len(written)} of them)",
      not verb_bad, ", ".join(verb_bad[:6]))
print(f"  note {len(guessed)} call(s) do not write a method down; only their path is checked")

# A parser that quietly stopped matching would leave the check above passing over
# an empty list, which is the failure mode of every check that reads source. So
# the number of call sites is compared with the number read: `ctx.rest(` appears
# 40-odd times in the merged panel, and a handful of them are built by hand
# (`ctx.rest(ep, ...)`, a helper's own URL) rather than written out.
# A parser that quietly stopped matching would leave the check above passing over
# an empty list, which is the failure mode of every check that reads source. The
# floor is the number of call sites whose path is written out; the rest are
# helpers forwarding an argument of their own, and they are named by line.
check(f"the calls were read, not skipped ({sites - len(unread)} of {sites} sites name their path)",
      sites - len(unread) >= 80, f"read {sites - len(unread)}")
print(f"  note {len(unread)} site(s) forward a path from an argument (lines {unread})")

# Declared but never called from the UI is information, not a failure: those are
# the agent's routes and the ones reached with a computed path.
unused = sorted(d for d in api_routes
                if not any(matches(b + u, d) for b in bases for u in ui_urls))
print(f"  note {len(unused)} routes are reached only with a computed path")

# --- the rename, from the outside in -----------------------------------------
check("the terminal route is the one the UI asks for",
      "/apps/{}/terminal" in ui_urls,
      str(sorted(u for u in ui_urls if "terminal" in u or u.endswith("/logs"))))
check("the UI no longer asks for the old logs route",
      not any(u.endswith("/logs") for u in ui_urls))

for word, why in (("Logs", "the user-facing word"), ("logsApp", "the old atom"),
                  ("LogsTab", "the old component"), ("openLogs", "the old opener"),
                  ("r.logs", "the old payload key")):
    check(f"no {word} left in the merged UI ({why})", word not in ui_src)

# Terminal used to be spelled out on the face of every card. It is now the second
# button while an app is idle and a menu entry while it runs — both fed by
# `appActions` — so the thing worth checking is that the card can still reach the
# handler, not that one literal string sits somewhere in the JSX.
check("the app card offers a Terminal button",
      "terminal: h.openTerminal" in ui_src
      and "const ACTS = appHandlers({" in ui_src
      and "() => pick(acts.second)" in ui_src,
      "Terminal is the second button while idle and a menu entry while running")
check("the tab is labelled Terminal", "['terminal', 'Terminal']" in ui_src)
check("closing the terminal cannot strand the view on a gone tab",
      "termApp.set(null); tab.set('apps')" in ui_src)
check("the tab bar is built by shellTabs, from the app AND the current job",
      "const tabs = shellTabs(useValue(termApp), useValue(activeJob), useValue(watchMachine))" in ui_src,
      "a shelf-wide update has output but no app, and still needs the terminal tab")
# --- no route wears the engine's name --------------------------------------
# The reverse-proxy to the engine used to sit at /pinokio/{path:path}. Nothing
# called it, and the name was the engine's rather than Athena's, so it is gone --
# and a route that comes back under that name should fail here.
check("no route is named after the engine",
      not any("pinokio" in r.lower() for r in api_routes),
      str(sorted(r for r in api_routes if "pinokio" in r.lower())))
check("the engine reverse-proxy is gone",
      "async def proxy(" not in api_src and "urlopen(req, timeout=120)" not in api_src)

check("the terminal branch is rendered exactly once",
      ui_src.count("t === 'terminal' ?") == 1, str(ui_src.count("t === 'terminal' ?")))

# --- route order, which FastAPI decides and nothing else does ----------------
# FastAPI answers with the FIRST route that matches, so a literal path declared
# after a catch-all of the same width is dead code: `/apps/{app_id}/{action}`
# matches `/apps/{app_id}/activity` too. The activity heartbeat lived below the
# catch-all and was dispatched as the action "activity", which sent the engine
# looking for an activity.js that has never existed. Cheap to check, invisible
# to every other test here (the route exists — it just cannot be reached).
decls = [(api_src[:m.start()].count("\n") + 1, m.group(1), m.group(2))
         for m in re.finditer(r'@router\.(get|post|put|delete|patch)\(\s*"([^"]+)"', api_src)]

# The general form of the rule, not just the one route that broke: a literal
# path declared AFTER a parameterized path of the same width and method is dead
# code, because FastAPI answers with the first route that matches. `/apps/updates`
# (two segments, GET) and `/apps/{app_id}` (two segments, GET) are the same route
# as far as the router is concerned, and only their order decides which runs.
def _would_match(pattern: str, literal: str) -> bool:
    a, b = pattern.split("/"), literal.split("/")
    if len(a) != len(b):
        return False
    return all(x == y or x.startswith("{") for x, y in zip(a, b))

dead = []
for n, method, path in decls:
    if "{" not in path:
        continue
    for n2, method2, path2 in decls:
        if n2 <= n or "{" in path2 or method2 != method:
            continue
        if _would_match(path, path2):
            dead.append(f"{method.upper()} {path2} (line {n2}) is unreachable — "
                        f"{method.upper()} {path} (line {n}) matches it first")
check("no literal route is shadowed by a parameterized route declared above it",
      not dead, "; ".join(dead))
catch_all = [(n, m, p) for n, m, p in decls if "{action}" in p]
if catch_all:
    cn, cm, cp = catch_all[0]
    after = [(n, p) for n, m, p in decls
             if n > cn and m == cm and p.count("/") == cp.count("/") and "{" not in p]
    check("the activity heartbeat is above the catch-all",
          not any("activity" in p for n, p in after), str(after))

print(f"\n{failed} failed" if failed else f"\n{passed}/{passed} checks passed")
sys.exit(1 if failed else 0)
