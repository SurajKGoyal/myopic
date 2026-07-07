# myopic

[![PyPI version](https://img.shields.io/pypi/v/myopic.svg)](https://pypi.org/project/myopic/)
[![Python](https://img.shields.io/pypi/pyversions/myopic.svg)](https://pypi.org/project/myopic/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP-Registry-7d6ad9.svg)](https://registry.modelcontextprotocol.io)

**The code-review MCP with the most ironic name in the registry.** It's anything
but nearsighted — it reviews your merge request against the *whole* codebase, not
just the diff in front of it.

<p align="center">
  <img src="https://raw.githubusercontent.com/SurajKGoyal/myopic/main/assets/demo.svg" alt="A small diff changes formatPrice to return a string; a diff-only reviewer says it looks fine, but myopic checks the whole repo and finds 4 callers it breaks plus an existing duplicate — neither visible in the diff." width="720">
</p>

> ⚠️ **Alpha / building in public.** Reviews **GitLab merge requests and GitHub
> pull requests** — pass either URL. Reads the change, reviews it against the
> whole codebase, and can post the review back as inline comments. Follow along,
> open issues, pitch in. Don't wire it into a critical workflow just yet.

---

## Why

Most AI code review looks at the diff in isolation. But the bugs that matter live
in what the diff *doesn't* show: the caller three files away that now breaks, the
convention every sibling file follows that this one quietly drops, the helper that
already exists so this new one is a duplicate. A reviewer that only reads the
patch is **myopic**.

myopic is an [MCP](https://modelcontextprotocol.io) server that feeds your AI
client (Claude, Cursor, …) the structured context to review like someone who
actually knows the codebase:

- **Read the change precisely** — the diff as line-numbered hunks or grouped by
  function/class, **token-safe on any MR size** (a 10,000-line diff never
  overflows the context window).
- **Review it against the whole codebase** — who calls the changed code (blast
  radius), the caller/callee graph, and — optionally — semantically similar code
  so you catch broken conventions and duplication.

It pairs with [amnesic](https://github.com/SurajKGoyal/amnesic), my MCP server
that gives AI persistent memory of SQL databases.

---

## Tools

Everything below **works today** unless marked planned.

**Read the merge request (token-safe by construction):**

| Tool | What it does |
|------|--------------|
| `mr_review_status` | MR metadata + every discussion thread + resolved/unresolved, in one call |
| `mr_changed_files` | a content-free manifest of changed files (paths, stats, noise flags) — always fits, any MR size |
| `mr_diff_sections` | the diff grouped by function/class (AST-aware), budget-bounded |
| `mr_diff_lines` | the diff as line-numbered hunks — exact positions for inline comments — budget-bounded |

On a large MR, the diff tools return a bounded page and list the rest under
`omitted_files` / `truncated` instead of failing; lockfiles, generated code, and
binaries are listed but not expanded. Fetch the rest with `files_filter`.

**Review against the whole codebase (point at a local clone):**

| Tool | What it does |
|------|--------------|
| `dependency_impact` | everywhere a changed symbol is used — the blast radius (ripgrep + tree-sitter) |
| `trace_call_chain` | the caller/callee graph of a symbol |
| `mr_review_context` | **the headline** — for each changed symbol: its impact (always), plus semantically similar code when the optional layer is enabled |

**Optional semantic layer** (`myopic[semantic]`) — `index_repo`, `code_search`,
and the semantic half of `mr_review_context`. See below.

**Close the loop — verify, and (on request) comment:**

| Tool | What it does |
|------|--------------|
| `mr_verify_review` | for each existing review thread, the diff changes near the commented line — did a follow-up commit address it? (read-only) |
| `mr_post_comments` | **the one write** — post inline comments, one at a time from a queue with exponential backoff (no drafts, no bulk-publish), so partial progress survives and rate limits are respected |

See [ROADMAP.md](./ROADMAP.md) for what's next.

---

## Install

[pipx](https://pipx.pypa.io) installs myopic isolated and on your PATH:

```bash
pipx install "myopic[semantic]"     # omit [semantic] for the core-only install
```

Prefer a plain venv? `python3 -m venv ~/.venvs/myopic && ~/.venvs/myopic/bin/pip
install "myopic[semantic]"`, then use that binary where the examples say `myopic`.

## Setup

myopic needs a personal access token with `api` (or `read_api`) scope. The wizard
walks you through it:

```bash
myopic init     # prompts for URL + token, verifies, saves both
myopic test     # ✓ Authenticated to https://gitlab.com as <you>
myopic doctor   # health-check config + (if enabled) the semantic layer
```

The token is saved to `~/.config/myopic/.env` (chmod 600) and referenced from the
TOML as `${GITLAB_TOKEN}` — never in the config file itself. Rotate it with
`myopic set-secret`, or hand-edit via `myopic init --template`.

**GitHub PRs:** just pass a PR URL. Set a `GITHUB_TOKEN` (a PAT with
pull-request read access) in your environment or a `[github]` section in
`config.toml`. For GitHub Enterprise, set `[github].url` to your host.

## Add to your AI client

Point any MCP client — Claude Code, Cursor, Claude Desktop — at the `myopic`
command:

```json
{
  "mcpServers": {
    "myopic": {
      "command": "myopic"
    }
  }
}
```

If your client can't find it on PATH, use the absolute path (pipx installs to
`~/.local/bin/myopic`).

### Configure inline instead of `myopic init`

Put the token in the `env` block and skip the config file — myopic reads
`GITLAB_TOKEN` / `GITHUB_TOKEN` from the environment:

```json
{
  "mcpServers": {
    "myopic": {
      "command": "myopic",
      "env": { "GITLAB_TOKEN": "glpat-…", "MYOPIC_AUTO_PULL": "1" }
    }
  }
}
```

`MYOPIC_AUTO_PULL=1` (optional) pulls a missing embedding model on first use
instead of erroring.

## Use

Point your AI at a merge request:

> "Review this MR: https://gitlab.com/group/project/-/merge_requests/42"

A good flow the client can follow: `mr_changed_files` to see the shape →
`mr_diff_sections` (large MRs) or `mr_diff_lines` to read the change → then, with
a local clone checked out, `dependency_impact` / `trace_call_chain` (or
`mr_review_context`) on the risky changed symbols to review against everything
that depends on them.

The graph tools analyze whatever is checked out at `root`, so check out the MR's
branch first — otherwise you're reviewing the target branch, and the MR's new
code isn't there. `myopic worktree <mr-url> <repo>` checks out the MR head in a
throwaway worktree (your main checkout untouched) and prints the path to use as
`root`. `mr_review_context` also warns when `root` doesn't hold the MR's head.

---

## Optional: semantic search (`myopic[semantic]`)

For "is this consistent with the rest of the codebase?" — duplication, convention
drift, similar patterns — enable the semantic layer. It's **opt-in** so the base
install stays lean (no torch, no heavyweight vector DB).

Embeddings come from a [local Ollama](https://ollama.com) server **you** run —
your code never leaves your machine. myopic talks to Ollama over HTTP; it does
not bundle or launch it. The one-time prerequisites:

1. `pip install "myopic[semantic]"` — adds `lancedb` + `httpx`.
2. Ollama running (default `localhost:11434`, or set `MYOPIC_OLLAMA_URL`).
3. The embedding model pulled: `ollama pull unclemusclez/jina-embeddings-v2-base-code`.

`myopic doctor` checks all three and offers to pull the model for you.

Embeddings are stored in an embedded [LanceDB](https://lancedb.com) index with
hybrid (vector + full-text) search. `index_repo` indexes a checked-out repo and
`mr_review_context` enriches each changed symbol with semantically similar code.
Without the extra, `mr_review_context` still returns the structural (graph) signal.

**Indexing is incremental and freshness-aware.** The first `index_repo` is a full
build; after that only files whose content changed are re-embedded, so refreshing
is cheap. `index_status(root)` reports whether the index is fresh, `stale` (with
how many commits behind HEAD), or built on a different model — freshness is keyed
to the git commit it was indexed from, not wall-clock time. `code_search` and
`mr_review_context` carry that status so a stale index never silently degrades a
review; the AI is told to offer a refresh when it's stale.

myopic is a stdio server (no background process), so there's no built-in
scheduler — but `myopic index /path/to/repo` is the hook for one. Point cron or
launchd at it to keep an index fresh out of band:

```bash
# refresh hourly (incremental — usually seconds)
0 * * * * myopic index /path/to/repo
```

Override the model/endpoint with `MYOPIC_EMBED_MODEL` / `MYOPIC_OLLAMA_URL`.

---

## Configuration reference

| Source | Key | Notes |
|--------|-----|-------|
| `config.toml` | `[gitlab].url` | GitLab base URL (default `https://gitlab.com`) |
| `config.toml` | `[gitlab].token` | use `${GITLAB_TOKEN}` — don't hardcode |
| `.env` (next to config) | `GITLAB_TOKEN` | the actual token value (chmod 600) |
| env var | `MYOPIC_GITLAB_URL` / `GITLAB_URL` | fallback if no TOML |
| env var | `MYOPIC_GITLAB_TOKEN` / `GITLAB_TOKEN` | fallback if no TOML |
| env var | `MYOPIC_CONFIG` / `MYOPIC_HOME` | override the config file / directory |
| env var | `MYOPIC_EMBED_MODEL` / `MYOPIC_OLLAMA_URL` | semantic layer model + endpoint |
| env var | `MYOPIC_AUTO_PULL` | `1` to auto-pull a missing embedding model on first use (default off) |

## Security

- **One explicit write, everything else read-only.** Only `mr_post_comments`
  mutates a review, and only when you ask for it — every other tool just reads MR
  and repo data. The write is never speculative.
- **Your token stays local.** It lives in your `.env` / environment and is sent
  only to your configured GitLab instance — never to any third party.
- Auth errors are scrubbed so your token never leaks into error messages.
- The optional semantic layer runs entirely locally (your Ollama, an on-disk
  index) — your code is never sent to a third party.

## Development

```bash
pip install -e ".[dev]"          # + ".[semantic]" to work on the semantic layer
pytest                           # hermetic — no network, Ollama, or lancedb needed
```

## License

MIT © Suraj Goyal

<!-- MCP Registry ownership marker — do not remove. -->
mcp-name: io.github.SurajKGoyal/myopic
