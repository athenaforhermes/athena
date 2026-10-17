"""
What will this ask me for?

A scanner is only as good as its restraint, so most of what is checked here is
what it must *not* say: a bare `PORT=` in an .env template is not a credential, a
README talking about `OPENAI_API_KEY` is not the app asking, a name like
`MONKEY` is not a key, `AUTH_MODE` is a setting rather than a secret, and a scan
that ran out of budget must not report "runs entirely on this box" — half a read
that found nothing is a fact about the read.

The fixtures are written to a scratch folder, so every claim is checked against
text this file made rather than against whatever happens to be installed.

Usage: env -u PYTHONPATH <venv python> t_keyscan.py
"""
import importlib.util
import inspect
import json
import shutil
import sys
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")
SCRATCH = Path(__file__).resolve().parent / "_tmp_keyscan"
sys.path.insert(0, str(PLUG / "dashboard"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ks = load("athena_keyscan", PLUG / "dashboard" / "keyscan.py")

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}   {detail}")


def write(folder: Path, rel: str, text: str) -> None:
    p = folder / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


if SCRATCH.exists():
    shutil.rmtree(SCRATCH, ignore_errors=True)

# ---------------------------------------------------------------- the asks
print("-- an app that asks for keys, in the shapes apps actually use --")
asks = SCRATCH / "asks.git"
write(asks, "install.js", """const port = process.env.PORT || 7860
const key = process.env.OPENAI_API_KEY
const r2 = process.env["REPLICATE_API_TOKEN"]
""")
write(asks, "app/run.py", """import os
key = os.environ["ANTHROPIC_API_KEY"]
alt = os.getenv('ELEVENLABS_API_KEY')
""")
write(asks, "start.sh", 'exec python app/run.py --token ${HF_TOKEN}\n')
write(asks, ".env.example", "OPENAI_API_KEY=\nPORT=7860\nMODEL=flux\nAUTH_MODE=local\n")
r = ks.scan(asks)

names = {k["name"] for k in r["keys"]}
check("every spelling of an environment read is found",
      {"OPENAI_API_KEY", "REPLICATE_API_TOKEN", "ANTHROPIC_API_KEY",
       "ELEVENLABS_API_KEY", "HF_TOKEN"} <= names, str(sorted(names)))
check("a bare setting in a template is not a credential",
      "PORT" not in names and "MODEL" not in names, str(sorted(names)))
check("a switch about credentials is a setting, not a secret",
      "AUTH_MODE" not in names, str(sorted(names)))
check("the owner of a key is worked out where the name says whose it is",
      {k["name"]: k["provider"] for k in r["keys"]}.get("HF_TOKEN") == "huggingface",
      str(r["keys"]))
check("a name that merely contains KEY is not a credential",
      ks._is_credential("MONKEY") is False and ks._is_credential("KEYBOARD") is False)
check("the same key named twice is one key",
      len([k for k in r["keys"] if k["name"] == "OPENAI_API_KEY"]) == 1
      and r["keys_total"] == len(r["keys"]), str(r["keys_total"]))
check("the evidence is quoted, so the claim can be looked at",
      all(k["line"] for k in r["keys"]), str(r["keys"]))
check("and the name is left in the quote, because the name is not the secret",
      any("OPENAI_API_KEY" in k["line"] for k in r["keys"]), str([k["line"] for k in r["keys"]]))

print("\n-- a value on the same line as the name --")
leaky = SCRATCH / "leaky.git"
# Built by concatenation so no secret-shaped literal sits in this file: the value
# under test is assembled at run time, and the check is that it never appears in
# what the scanner returns.
VAL_A = "sk-live-" + "abcdefghijklmnop"
VAL_B = "r8_live" + "0123456789abcdef"
VAL_C = "sk-" + "abcdefghijklmnopqrstuv"
write(leaky, "conf.py", 'OPENAI_API_KEY = ""\n')
write(leaky, "filled.py", 'REPLICATE_API_TOKEN = "' + VAL_A + '"\n')
write(leaky, "app.py",
      "token = os.getenv('REPLICATE_API_TOKEN', '" + VAL_B + "')\n")
write(leaky, "note.js", "// see https://api.replicate.com and the key " + VAL_C + "\n")
r_leak = ks.scan(leaky)
blob = json.dumps(r_leak)
check("a key written on its own line leaves the value out of the evidence",
      VAL_A not in blob and VAL_B not in blob, blob[:300])
check("a token inside a comment is masked too", VAL_C not in blob, blob[:300])
check("while the name the reader needs is still named",
      any(k["name"] == "OPENAI_API_KEY" for k in r_leak["keys"]), str(r_leak["keys"]))
check("a blank assignment is an ask, wherever it is written",
      any(k["name"] == "OPENAI_API_KEY" for k in r_leak["keys"])
      and "OPENAI_API_KEY" in json.dumps(r_leak), str(r_leak["keys"]))
check("a key with a real value in it is not an ask — the app already has one",
      all(k["name"] != "REPLICATE_API_TOKEN" or k["where"] != "filled.py"
          for k in r_leak["keys"]), str(r_leak["keys"]))
check("a placeholder is an ask, because someone still has to fill it in",
      ks._is_credential("OPENAI_API_KEY") is True
      and "PLACEHOLDER" in ks._ASSIGN_ASK.pattern.upper(), ks._ASSIGN_ASK.pattern[:60])
check("and a long run that is just a name is not mistaken for a value",
      "REPLICATE_API_TOKEN" in json.dumps([k["line"] for k in r_leak["keys"]]),
      str([k["line"] for k in r_leak["keys"]]))

print("\n-- whether Hermes already answers that ask --")
held = ks.scan(asks, held={"openai", "huggingface"})
by_name = {k["name"]: k for k in held["keys"]}
check("a key Hermes holds is reported as answered",
      by_name["OPENAI_API_KEY"]["held"] is True, str(by_name["OPENAI_API_KEY"]))
check("a key for a provider Hermes does not hold is not",
      by_name["ANTHROPIC_API_KEY"]["held"] is False, str(by_name["ANTHROPIC_API_KEY"]))
check("the count of answered keys matches the list",
      held["held_count"] == len([k for k in held["keys"] if k["held"]]),
      f'{held["held_count"]} vs {held["keys"]}')
check("answered keys sort first, so a card can show the ones that matter",
      [k["held"] for k in held["keys"]] == sorted([k["held"] for k in held["keys"]],
                                                 reverse=True), str(held["keys"]))
check("an app with keys is not called local",
      held["local"] is False, str(held["local"]))

# ---------------------------------------------------------------- local only
print("\n-- an app that runs on this box --")
local = SCRATCH / "local.git"
write(local, "app/main.py", """import os
model = os.environ.get("MODEL_PATH", "./models")
host = os.environ.get("GRADIO_SERVER_NAME", "127.0.0.1")
""")
write(local, "install.py", "print('nothing to sign up for')\n")
r2 = ks.scan(local)
check("an app with no key and no cloud host is called local", r2["local"] is True, str(r2))
check("and it says the read was complete before making that claim",
      r2["complete"] is True and r2["capped"] is False, str(r2))
check("ordinary settings are counted without being called asks",
      r2["env_count"] == 2 and r2["keys_total"] == 0, str(r2))

print("\n-- a hardcoded endpoint asks just as loudly as a key --")
cloud = SCRATCH / "cloud.git"
write(cloud, "run.mjs", """const url = 'https://api.replicate.com/v1/predictions'
await fetch(url, {})
""")
r3 = ks.scan(cloud)
check("a literal cloud host is found without any key name",
      r3["cloud_total"] == 1 and r3["cloud"][0]["host"] == "api.replicate.com", str(r3["cloud"]))
check("so the app is not called local", r3["local"] is False, str(r3["local"]))
write(SCRATCH / "hf.git", "get.py",
      "from huggingface_hub import hf_hub_download\n"
      "hf_hub_download('stabilityai/sd-vae-ft-mse', 'config.json')\n")
check("a download from huggingface.co is the local path, not a call-out",
      ks.scan(SCRATCH / "hf.git")["local"] is True, str(ks.scan(SCRATCH / "hf.git")))

# ---------------------------------------------------------------- restraint
print("\n-- what the scanner refuses to read --")
noisy = SCRATCH / "noisy.git"
write(noisy, "README.md", "Set OPENAI_API_KEY in your environment to begin.\n")
write(noisy, ".env.example", "PORT=3000\n")
write(noisy, "node_modules/dep/index.js", "process.env.STRIPE_SECRET_KEY\n")
write(noisy, "env/lib/python3.11/site-packages/thing.py", "os.environ['AWS_SECRET_ACCESS_KEY']\n")
write(noisy, "app.py", "print('hi')\n")
r4 = ks.scan(noisy)
check("a README naming a key is not the app asking",
      r4["keys_total"] == 0, str(r4["keys"]))
check("a key inside an installed dependency is not this app's ask",
      r4["keys_total"] == 0 and r4["local"] is True, str(r4))
check("a folder it skipped is not a folder it failed on",
      r4["complete"] is True, str(r4))

print("\n-- a scan that ran out of budget says so --")
capped = SCRATCH / "big.git"
for i in range(ks.MAX_FILES + 5):
    write(capped, f"src/f{i}.py", "print(1)\n")
r5 = ks.scan(capped)
check("the walk stops at the file budget", r5["capped"] is True, str(r5["files_read"]))
check("and the app is not called local on half a read",
      r5["local"] is False and r5["complete"] is False, str(r5))
big = SCRATCH / "huge.git"
write(big, "data/train.json", "x" * (ks.MAX_FILE_BYTES + 10))
write(big, "app.py", "print('tiny')\n")
r6 = ks.scan(big)
check("an oversized dataset is skipped rather than read",
      r6["files_read"] == 1, str(r6["files_read"]))
check("and a dataset nobody read does not weaken the answer",
      r6["complete"] is True and r6["local"] is True, str(r6))
huge_script = SCRATCH / "hugescript.git"
write(huge_script, "bundle.py", "# pad\n" + "x = 1\n" * (ks.MAX_FILE_BYTES // 3))
write(huge_script, "app.py", "print('tiny')\n")
r7 = ks.scan(huge_script)
check("an oversized script is the one skip that does weaken it",
      r7["complete"] is False and r7["local"] is False, str(r7))
check("and the script that was not read is named",
      r7["skipped_large"] == ["bundle.py"], str(r7["skipped_large"]))

print("\n-- what a card gets --")
summary = ks.summarise(r)
check("the summary carries the state, not the evidence",
      summary["scanned"] is True and summary["local"] is False
      and summary["keys_total"] == len(r["keys"])
      and "line" not in (summary["keys"][0] if summary["keys"] else {}), str(summary))
check("nothing scanned reads as unknown, never as local",
      ks.summarise(None) == {"scanned": False, "local": None, "keys": [], "keys_total": 0,
                             "held_count": 0, "cloud_total": 0, "complete": False},
      str(ks.summarise(None)))

print("\n-- the cache is a read, not a re-walk --")
home = SCRATCH / "home"
ks.scan_and_save(home, asks, held={"openai"})
back = ks.load(home, asks.name)
check("a scan survives as a file beside the plugin's own home",
      ks.cache_path(home, asks.name).is_file() and back is not None, str(back)[:80])
check("and it comes back with the same verdict",
      back["keys_total"] == r["keys_total"] and back["local"] == r["local"]
      and bool(back.get("scanned_at")), str(back)[:120])
check("a folder name with a path separator cannot escape the cache dir",
      "/" not in ks.cache_path(home, "../../evil").name
      and ks.cache_path(home, "../../evil").parent == ks.cache_dir(home),
      str(ks.cache_path(home, "../../evil")))
check("a missing cache is None rather than an exception",
      ks.load(home, "never-scanned") is None)
check("the cache holds no secret value, only names and lines",
      "sk-" not in json.dumps(back) and "Bearer" not in json.dumps(back))

print("\n-- the real machine, if it has apps --")
api_root = Path.home() / "AppData/Local/hermes/athena/api"
real = sorted(p for p in api_root.glob("*") if p.is_dir()) if api_root.is_dir() else []
if real:
    first = real[0]
    rr = ks.scan(first, held={"huggingface", "github"})
    check(f"a real app folder reads without raising ({first.name})",
          rr["ok"] is True and rr["files_read"] >= 0, str(rr)[:200])
    check("and its verdict is a real one, not a blank",
          rr["local"] in (True, False) and rr["complete"] in (True, False), str(rr)[:120])
else:
    print("  skip no app folders on this machine")

print("\n-- the walk is bounded by the cap, not by the tree --")
big = SCRATCH / "walk"
(big / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
(big / "venv" / "lib").mkdir(parents=True, exist_ok=True)
for i in range(60):
    (big / "node_modules" / "pkg" / f"dep{i}.js").write_text("process.env.DEP_KEY\n", encoding="utf-8")
    (big / "venv" / "lib" / f"mod{i}.py").write_text("os.environ.get('VENV_KEY')\n", encoding="utf-8")
(big / "app.py").write_text("import os\nos.environ['APP_KEY']\n", encoding="utf-8")
(big / "zzz.py").write_text("import os\nos.environ['LAST_KEY']\n", encoding="utf-8")
_cand = ks._candidates(big)
check("the walk is a generator, so nothing is enumerated before the cap applies",
      inspect.isgenerator(_cand), type(_cand).__name__)
_walked = ks._files(big)
_names = [p_.name for p_ in _walked[0]]
check("... and it never descends into a dependency folder",
      "app.py" in _names and "zzz.py" in _names
      and not any(n.startswith("dep") or n.startswith("mod") for n in _names), str(_names[:8]))
check("... with the files it does return in a stable order", _names == sorted(_names), str(_names[:8]))
_seen = ks.scan(big, folder_name="walk")
_seen_names = {k.get("name") for k in (_seen.get("keys") or [])}
check("... so a scan of that folder sees the app's own keys and nothing from its dependencies",
      {"APP_KEY", "LAST_KEY"} <= _seen_names
      and not any(str(n).startswith(("DEP_KEY", "VENV_KEY")) for n in _seen_names),
      str(sorted(x for x in _seen_names if x)))
check("... and the file cap still applies to what it does collect",
      len(ks._files(big)[0]) <= ks.MAX_FILES and isinstance(_walked[1], bool))

# Nothing this test wrote is left behind: a scratch folder under the plugin is a
# scratch folder in the repository, and the next run would read last run's cache
# and pass for the wrong reason.
shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\nt_keyscan: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
