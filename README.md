# Athena — the local AI app store for Hermes

Athena browses a constantly growing catalogue of apps (image, video, audio, LLM, agent projects)
and installs them **natively inside Hermes Desktop**. It is a single unified Hermes plugin: one
package, one engine, one credential set.

No second window to drive. No second login. If Hermes already holds a GitHub or HuggingFace
credential, Athena uses it.

## What you get

- **Local AI apps, installed and run from inside Hermes.** Browse a catalogue of ~6,400 apps,
  install one, launch it, watch its log, stop it. Nothing needs an account and Athena has no API
  keys of its own; an app that wants one is labelled *needs a key* before you launch it, and it
  reuses the credential Hermes already holds when it can.
- **The apps you already have come with you.** Point it at a folder of apps installed elsewhere and
  they appear as cards — **linked, not copied, by default**, so the originals stay where they are.
  See `Import from an existing install` in Settings.
- **Everything is one click.** Install from any git repository, launch it, read its log, stop
  it — the whole loop without a terminal, and a stopped app's own log names the cause.
- **Agent control is in active development.** This release is the features above, done well;
  driving all of it from a single sentence is the next one.
- **Make your own.** A git repository becomes an app you can run from the panel:
  `docs/build-an-app.md`.

## Why it exists

Hermes already has the agent, the tools, and the credentials. What it lacked was a place to
*run* the rest of the local AI ecosystem. Athena embeds a proven MIT-licensed app runtime
([upstream here](https://www.npmjs.com/package/pinokiod)) in `engine/`, vendored unmodified, and
puts Hermes' own UI, naming and agent interface on top of it.

## Architecture

Athena is **its own front end over a shared runtime**:

- **Own UI.** `desktop/plugin.js` draws every card, list, and button. Nothing upstream is
  embedded, framed, or re-skinned, and no upstream branding appears anywhere in the panel.
- **Catalogue as a data source.** `dashboard/catalog.py` reads the public store index and
  serves clean records (name, repo, version, icon, description). Athena renders them.
- **Shared runtime.** The vendored engine does the actual installing and running;
  Apps install as ordinary git clones, so upstream fixes to the runtime benefit Athena
  automatically.
- **Bring your own app.** Any git repository can become an Athena app from the Store tab.

The catalogue index (every app the store lists) is fetched once and cached for a day;
individual app records are cached for a week. Browse works with no network at all.

