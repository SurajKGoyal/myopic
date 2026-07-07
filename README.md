# myopic

[![PyPI version](https://img.shields.io/pypi/v/myopic.svg)](https://pypi.org/project/myopic/)
[![Python](https://img.shields.io/pypi/pyversions/myopic.svg)](https://pypi.org/project/myopic/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP-Registry-7d6ad9.svg)](https://registry.modelcontextprotocol.io)

**The code-review MCP with the most ironic name in the registry.** It's anything
but nearsighted — it reviews your merge request against the *whole* codebase, not
just the diff in front of it.

> ⚠️ **Alpha / building in public.** Reviews **GitLab merge requests and GitHub
> pull requests** — pass either URL. The review tools work now; comment-posting
> is on the roadmap. Follow along, open issues, pitch in. Don't wire it into a
> critical workflow just yet.

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

**Planned:** bulk inline-comment posting. See [ROADMAP.md](./ROADMAP.md).

---

## Install

```bash
uvx myopic                       # run directly (recommended)
# or
pip install myopic
```

## Setup

myopic needs a GitLab URL and a personal access token with `api` (or `read_api`)
scope. The interactive wizard walks you through it:

```bash
myopic init      # prompts for URL + token, verifies the connection, saves both
myopic test      # ✓ Authenticated to https://gitlab.com as <you>
```

The token is saved to `~/.config/myopic/.env` (chmod 600) and referenced from the
TOML as `${GITLAB_TOKEN}` — it never lives in the config file. Rotate it any time
with `myopic set-secret`. Prefer to hand-edit? `myopic init --template`.

**Reviewing GitHub PRs?** myopic reviews GitHub pull requests too — just give it
a PR URL. It needs a **GitHub token** (a PAT with pull-request read access). Set
it via `GITHUB_TOKEN` in your environment / the `.env`, or add a `[github]`
section to `config.toml` (see `myopic init --template`). For GitHub Enterprise,
set `[github].url` to your instance host. Public github.com needs no URL.

## Add to your AI client

**Claude Code** (`~/.claude/mcp.json` or project `.mcp.json`), Cursor, Claude
Desktop, and other MCP clients all use the same command:

```json
{
  "mcpServers": {
    "myopic": {
      "command": "uvx",
      "args": ["myopic"]
    }
  }
}
```

## Use

Point your AI at a merge request:

> "Review this MR: https://gitlab.com/group/project/-/merge_requests/42"

A good flow the client can follow: `mr_changed_files` to see the shape →
`mr_diff_sections` (large MRs) or `mr_diff_lines` to read the change → then, with
a local clone checked out, `dependency_impact` / `trace_call_chain` (or
`mr_review_context`) on the risky changed symbols to review against everything
that depends on them.

---

## Optional: semantic search (`myopic[semantic]`)

For "is this consistent with the rest of the codebase?" — duplication, convention
drift, similar patterns — enable the semantic layer. It's **opt-in** so the base
install stays lean (no torch, no heavyweight vector DB):

```bash
pip install "myopic[semantic]"   # adds lancedb + httpx only
ollama pull unclemusclez/jina-embeddings-v2-base-code   # a small, code-specialized model
```

It embeds your code locally via [Ollama](https://ollama.com) and stores it in an
embedded [LanceDB](https://lancedb.com) index with native hybrid (vector + full-text)
search. Then the AI can `index_repo` a checked-out repo and `mr_review_context`
will enrich each changed symbol with semantically similar code. Without the extra,
`mr_review_context` still works — it just returns the structural (graph) signal.

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

## Security

- **Read-only today.** The shipped tools only *read* MR and repo data; nothing
  posts or mutates. (Comment-posting on the roadmap will be explicit and opt-in.)
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
