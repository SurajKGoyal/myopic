# myopic

**The code-review MCP with the most ironic name in the registry.** It's anything
but nearsighted — the goal is to review your merge request against the *whole*
codebase, not just the diff in front of it.

> ⚠️ **Alpha / building in public.** myopic is under active development and
> currently supports **GitLab merge requests only** — GitHub pull requests are
> on the roadmap (the platform layer is already abstracted for them). The
> roadmap below is public on purpose: follow along, open issues, and pitch in.
> What's checked off works today; the rest is coming. Don't depend on it in a
> critical workflow yet.

---

## Why

Most AI code review looks at the diff in isolation. But the bugs that matter
live in what the diff *doesn't* show: the caller three files away that now
breaks, the convention every sibling file follows that this one quietly drops,
the helper that already exists so this new one is a duplicate, the file nobody
imports. A reviewer that only reads the patch is **myopic**.

myopic is an [MCP](https://modelcontextprotocol.io) server that feeds your AI
client (Claude, Cursor, etc.) the structured context to review like someone who
actually knows the codebase — starting with precise, line-numbered access to the
merge request itself, and growing toward full codebase-aware review (see the
roadmap).

It pairs with [amnesic](https://github.com/SurajKGoyal/amnesic), my MCP server
that gives AI persistent memory of SQL databases.

---

## Status & roadmap

| Tool | What it does | Status |
|------|--------------|--------|
| `mr_review_status` | MR metadata + every discussion thread + resolved/unresolved state, in one call | ✅ alpha |
| `mr_diff_lines` | MR diff as structured, line-numbered hunks — exact positions for inline comments | ✅ alpha |
| `mr_diff_sections` | AST-grouped diff (by function/class) so large MRs don't blow the token budget | 🔜 planned |
| `review_with_context` | RAG-augmented review: callers of changed symbols + conventions sibling files follow + duplication + unwired-file detection | 🔜 planned |
| `mr_post_comments` | bulk-post inline review comments at exact diff positions | 🔜 planned |
| GitHub pull requests | same tools, GitHub PRs (the platform layer is already abstracted for this) | 🔜 planned |

Full details and design notes: [ROADMAP.md](./ROADMAP.md). Ideas and issues
welcome — that's the point of building this in the open.

---

## Install

```bash
uvx myopic            # run directly (recommended)
# or
pip install myopic
```

## Setup

myopic needs a GitLab URL and a personal access token with `api` (or `read_api`)
scope.

```bash
myopic init           # writes a config template + prints next steps
```

Then put your token in `~/.config/myopic/.env`:

```
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
```

Edit `~/.config/myopic/config.toml` if your GitLab is self-hosted (defaults to
`https://gitlab.com`). Verify it:

```bash
myopic test           # ✓ Authenticated to https://gitlab.com as <you>
```

The token never lives in the TOML — it's referenced as `${GITLAB_TOKEN}` and
read from the `.env` file (or your environment).

## Add to your AI client

**Claude Code** (`~/.claude/mcp.json` or project `.mcp.json`):

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

The same `command`/`args` work for Claude Desktop, Cursor, and other
MCP-compatible clients — consult your client's MCP config docs for where the
file lives.

## Use

Point your AI at a merge request:

> "Review this MR: https://gitlab.com/group/project/-/merge_requests/42"

Your client will call `mr_review_status` to orient, then `mr_diff_lines` to read
exactly what changed with precise line numbers — and review from there.

---

## Configuration reference

| Source | Key | Notes |
|--------|-----|-------|
| `config.toml` | `[gitlab].url` | GitLab base URL (default `https://gitlab.com`) |
| `config.toml` | `[gitlab].token` | use `${GITLAB_TOKEN}` — don't hardcode |
| `.env` (next to config) | `GITLAB_TOKEN` | the actual token value |
| env var | `MYOPIC_GITLAB_URL` / `GITLAB_URL` | fallback if no TOML |
| env var | `MYOPIC_GITLAB_TOKEN` / `GITLAB_TOKEN` | fallback if no TOML |
| env var | `MYOPIC_CONFIG` | override the config file path |
| env var | `MYOPIC_HOME` | override the config directory |

## Security

- **Read-oriented today.** The shipped tools only *read* MR data; nothing posts
  or mutates. (Comment-posting on the roadmap will be explicit and opt-in.)
- **Your token stays local.** It lives in your `.env` / environment and is sent
  only to your configured GitLab instance — never to any third party.
- Auth errors are scrubbed so your token never leaks into error messages.

## Development

```bash
pip install -e ".[dev]"
pytest                 # diff-parser unit tests, no network needed
```

## License

MIT © Suraj Goyal

<!-- MCP Registry ownership marker — do not remove. -->
mcp-name: io.github.SurajKGoyal/myopic
