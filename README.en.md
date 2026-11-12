# <img src="docs/logo.svg" width="26"> single-file-wbs

> A single-file viewer that runs **the plan (WBS) and the issues in one page**.
> No server, no libraries, no build step. Open it in Chrome straight from `file://`.

The on-screen application name is **WBS Viewer** (`single-file-wbs` is the distribution name — this repository).

**[日本語版 README / Japanese README](README.md)**

![plan](docs/screenshot.en.png)

Plan: a Gantt that overlays actuals on the planned frame, with the *inazuma* (slip) line breaking left of today.

![issues](docs/screenshot-issues.en.png)

Issues: flip **Plan｜Issues** in the toolbar and the same file's issue table appears.

## Concept

- **One HTML file and one JSON file**
  Open via `file://`; saving writes back to that same file (File System Access API).
- **Humans edit via the screen, AI edits via raw JSON — the same table**
  The bundled [`AGENTS.en.md`](AGENTS.en.md) is not a read-me; it is the spec written for AI.
- **The plan holds "when, who, how much"; the issues hold "what, why, and what makes it done"**
  Only the issue side carries links, and the screen jumps both ways (nothing is written on the plan side).

## Start in 30 seconds

**Open it in your browser** (no download, no install)

- Demo → **<https://piguo45.github.io/single-file-wbs/demo.html>** (the sample plan and issues show up the moment it opens; a fictional project whose dates move to the day you open it)
- The tool → **<https://piguo45.github.io/single-file-wbs/wbs_viewer.html>** (open it and drop your own `wbs.json` on it; your data is processed inside the browser only and is never sent anywhere)

**Keep a local copy**

1. Download `wbs_viewer.html` from [Releases](https://github.com/piguo45/single-file-wbs/releases/latest)
2. Open it in Chrome (plain `file://` is fine)
3. Load `wbs_sample_issues.json` via **Open file** (or drag & drop onto that same button)

Copy a sample as the template for your own data. Edit, save, then press **Reload**.
Edit mode (editing on the screen) asks you to re-pick the same file once per Chrome session — that selection is how the browser grants write permission (steps are in [`AGENTS.en.md`](AGENTS.en.md)).

## What it does

Plan (WBS)

- **Planned-vs-actual Gantt overlay** — actual bars sit on the planned frame; finish slip (red, +N days) and late starts read at a glance
- **Progress-axis view (EVM-style)** — a tab whose x-axis is completion; actual (EV), planned (PV) and behind are shown as bars
- **Inazuma (slip) line** — rows breaking left of the today line are behind
- **Reschedule history** — `↷` records a plan change with its reason. You type only actual dates; effort, progress and the lines are derived
- **Dependency tab** — a fourth axis showing task order as circles and arrows; the longest path is red, and a task fills red if it's scheduled to start before its predecessor finishes (details in `AGENTS.en.md`)

Issues

- **Two questions and a close condition** — what breaks if ignored / what you gain if done, and what state proves it is finished
- **Undecided, waiting and frozen marks** — the state is only Not started / In progress / Closed; the reason for a stall stacks on top as a mark
- **★ Updated and ⚠ Nudge** — rows that moved during the reporting window get ★; an overdue wait or open question gets ⚠ Nudge
- **Moving between plan and issues** — `WBS 2.3` on an issue and the `Issue #3` tag on a task move you within the same page

## Asking an AI

The data is one plain JSON file, so you can hand updates to a chat.

- "2.3 started" → today goes into `actual.start`
- "Put #3 on waiting — the dev team's answer, by 9/12" → `pending` gets who / what / by when, and past 9/12 an `⚠ Nudge` appears
- "Report this round's ★" → the issues that moved during the reporting window, as a list (the JSON is not changed)

The conventions live in [`AGENTS.en.md`](AGENTS.en.md). An AI reads that before touching the data.

## Open it inside Claude (MCP App)

The same screen can be shown right inside a Claude conversation (any host that supports [MCP Apps](https://github.com/modelcontextprotocol/ext-apps)).
The bundled MCP server [`scripts/mcp_server.py`](scripts/mcp_server.py) serves `wbs_viewer.html` **unmodified** (Python standard library only — zero dependencies).

**Claude Desktop (reads your local file directly)** — add this to `claude_desktop_config.json` and restart:

```json
{
  "mcpServers": {
    "wbs": {
      "command": "python3",
      "args": ["/path/to/single-file-wbs/scripts/mcp_server.py", "/path/to/wbs.json"]
    }
  }
}
```

Ask "show me the WBS" and `wbs_open` is called: the Gantt chart and the issue table appear in the conversation.
**Edit and Refresh work as usual** (the server writes to the same JSON). Add `--read-only` to make it view-only.

**claude.ai (custom connector)** — listen on HTTP and expose it through a tunnel:

```bash
python3 scripts/mcp_server.py /path/to/wbs.json --http 8765 --read-only --token "$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
npx cloudflared tunnel --url http://127.0.0.1:8765
# connector URL = https://<tunnel host>/mcp/<token>
```

- **`--token` is required when you expose it** (anyone who knows the URL can read — and write, unless `--read-only`). Without a token the server only answers requests from your own machine.
- If the JSON changed outside the view (an AI, an editor), a save is **refused rather than overwritten**. Press Refresh to reload.
- How it works and what has been verified → [`docs/design/mcp-app.md`](docs/design/mcp-app.md) (Japanese)

## More

- Spec and AI conventions → [`AGENTS.en.md`](AGENTS.en.md) (Japanese original: [`AGENTS.md`](AGENTS.md))
- Design documents and ADRs → [`docs/`](docs/index.md) ([overview](docs/design/system-overview.md), [ADRs](docs/adr/))
- Running the regression suites → [`tests/e2e/README.md`](tests/e2e/README.md) (plan), [`tests/e2e_issue/README.md`](tests/e2e_issue/README.md) (issues), [`tests/e2e_mcp/README.md`](tests/e2e_mcp/README.md) (MCP App)
- Requirements → Google Chrome (latest) recommended; Edge and other Chromium browsers work. Firefox and Safari are not supported (no File System Access API)
- Samples → [`wbs_sample.json`](wbs_sample.json) (plan only), [`wbs_sample_issues.json`](wbs_sample_issues.json) (plan + issues)
- Legend of states and marks → the **Legend** button on the issue toolbar
- Changelog → [Releases](https://github.com/piguo45/single-file-wbs/releases)

## License

[MIT](LICENSE)
