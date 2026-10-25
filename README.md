# Athena — the local AI app store for Hermes

Athena browses a constantly growing catalogue of apps (image, video, audio, LLM, agent projects)
and installs them **inside Hermes Desktop**. It is a single Hermes plugin: one package, one
engine, one credential set.

No second window to drive and no second login. When an app needs a GitHub or HuggingFace token,
Athena asks you once — per app — and only if you say yes does it hand that app the one Hermes
already holds *for you*: read through Hermes' own secret route and passed to the app as an
environment variable for the life of its process, never logged, never written to disk in
plaintext, and never sent anywhere by Athena itself. No token is read from another program's
store, and saying no leaves the app without one.

## What you get

- **Local AI apps, installed and run from inside Hermes.** Browse a catalogue of ~6,400 apps,
  install one, launch it, watch its log, stop it. Nothing needs an account and Athena has no API
  keys of its own; an app that wants one is labelled *needs a key* before you launch it.
- **The apps you already have come with you.** Point it at a folder of apps installed elsewhere and
  they appear as cards — **linked, not copied, by default**, so the originals stay where they are.
  See `Import from an existing install` in Settings.
- **Everything is one click.** Install from any git repository, launch it, read its log, stop
  it — the whole loop without a terminal, and a stopped app's own log names the cause.
- **Agent control is in active development.** This release is the features above, done well;
  driving all of it from a single sentence is the next one.
- **Make your own.** A git repository becomes an app you can run from the panel:
  `docs/build-an-app.md`.

## What it does on your machine — read this before installing

- **Third-party code runs as you.** Installing an app clones a repository you named and runs
  **that repository's own install script**, on this computer, with your files, your privileges and
  the credentials it is handed. Nothing here sandboxes it, and Athena cannot vouch for that code.
  Use the judgement you would use before running a stranger's script by hand.
- **A background engine runs.** One Node process, serving HTTP on **loopback only**, started when
  Athena needs it and stopped with it. It is what installs, launches and stops apps.
- **These are the endpoints it contacts.** The public catalogue (the store's API, its tag cloud,
  and an index file on GitHub), `github.com` and `api.github.com` when it clones an app or
  publishes one, `registry.npmjs.org` once when setup installs the engine, and `huggingface.co`
  only when you have given an app a token. A WSL guest image comes from
  `cloud-images.ubuntu.com`, and only if you ask for a machine. No telemetry of any kind.
- **Machines are real.** The Machines tab runs a WSL distribution or a QEMU guest on this computer
  and can **execute commands and send keystrokes inside it** on your behalf.
- **Publishing exists.** `athena_publish` pushes to a GitHub repository you name, using the GitHub
  credential Hermes already holds — read through Hermes' own secret route, the same way an app
  gets one. It asks before it pushes, and it only pushes where you told it to.

## Why it exists

Hermes already has the agent, the tools, and the credentials. What it lacked was a place to *run*
the rest of the local AI ecosystem. Athena puts Hermes' own UI, naming and agent interface on top
of a proven MIT-licensed app runtime ([on npm](https://www.npmjs.com/package/pinokiod)) — pinned
to one exact version, never forked, and **not vendored here**: the repository carries the engine's
manifest and lockfile, and setup installs that exact version from npm once you agree to it.

## Architecture

Athena is **its own front end over a shared runtime**:

- **Own UI.** `desktop/plugin.js` draws every card, list, and button. Nothing upstream is
  embedded, framed, or re-skinned, and no upstream branding appears anywhere in the panel.
- **Catalogue as a data source.** `dashboard/catalog.py` reads the public store index and
  serves clean records (name, repo, version, icon, description). Athena renders them.
- **Shared runtime.** The pinned engine does the actual installing and running;
  apps install as ordinary git clones, so upstream fixes reach Athena by raising the pin.
- **Bring your own app.** Any git repository can become an Athena app from the Store tab.

The catalogue index (every app the store lists) is fetched once and cached for a day;
individual app records are cached for a week. Browse works with no network at all.
