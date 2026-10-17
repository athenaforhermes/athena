# Athena Store

The app store for **Athena** — the local AI app store built into Hermes.

Athena installs and runs local AI apps (image, video, music, 3D, workflows) with
one click, in the same place its agent already works. This repository is where
the apps live and where the store's index is published, so Athena can read it as
its primary source: browse it in the app, or ask the agent for an app by name.

## Layout

```
main              index.json + this README — what the store reads
athena-<app>      one branch per app: athena.js (+ a one-line shim), install.js, start.js, …
```

**One branch per app, not one folder per app.** An installed app is a `git clone`
of its launcher repository, and every app's `update.js` runs `git pull` at its
root. An app that arrived as a folder of some larger repository has no history
to pull, so its Update button is dead the moment it is installed. A branch is a
real ref: clone it and you have a genuine checkout that can pull the app's next
version.

`index.json` on `main` is the catalogue: one record per app, in the store's own
record shape — `slug`, `title`, `description`, `version`, `iconUrl`, `repoUrl`,
`tags` — plus the fields the store needs to install from a branch.

## Install

In Athena: Store → the app → Install.

From the agent (Tala), same thing:

```
athena_install(url="athenaforhermes/athena-store#athena-<app>")
```

Anywhere git is available:

```
git clone --branch athena-<app> https://github.com/athenaforhermes/athena-store.git <app>.git
```

The `#branch` form is what carries an app: `owner/repo#branch`. Plain
`owner/repo` clones the default branch.

## The apps

The catalogue is not open yet: `index.json` carries an empty `apps` list while
the requirements for listing an app are being worked out. This table fills in as
apps are published.

| App | Branch | What it does |
| --- | --- | --- |
| _none yet_ | — | — |

Each branch carries the app's own launcher files plus, where the app has been
converted, two files Athena writes:

- `GUIDE.md` — what the app can do, and how to run it, for a person or an agent
- `agent.json` — the same thing as data: capabilities, their inputs, their likely
  outputs, so an agent can call the app without being taught it first

Apps are not modified here. Upstream author and licence stay with the app; each
branch's commit names its upstream repository.

## Publishing an app

`tools/publish_store.py` reads installed apps and rewrites their branches:

```
python tools/publish_store.py --home <athena home> --out <this repo> --repo <owner/name>
```

It takes each app's tracked files (the launcher scripts, the app's own notes,
icons, wheels) plus `GUIDE.md`/`agent.json` when present, and leaves out the
build: virtualenvs, model weights, logs and generated output are untracked in the
app's own repository and stay on the machine that installed it.

Add an app by installing it in Athena, running the command above, then pushing
the branch.
