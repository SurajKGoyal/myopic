# myopic roadmap

myopic is built in the open and shipped in small increments. This file is the
honest source of truth for what works, what's next, and why. If something here
interests you, open an issue or a PR — design discussion is welcome.

The north star: **review a change against the whole codebase, not just the
diff.** Everything below ladders up to that.

---

## ✅ Shipped (v0.1.0)

**Read the merge request — token-safe on any MR size:**

- **`mr_review_status(url)`** — one snapshot of where a review stands: metadata,
  every discussion thread, resolved vs open, general comments, file-change summary.
- **`mr_changed_files(url)`** — a content-free manifest (paths, stats, noise flags)
  that stays small no matter how large the MR's diff — no content, just the map.
  The cheap entry point.
- **`mr_diff_sections(url)`** — the diff grouped by enclosing function/class
  (tree-sitter AST). Budget-bounded.
- **`mr_diff_lines(url, files_filter?, lines_filter?)`** — the diff as line-numbered
  hunks (exact positions for inline comments). Budget-bounded; `lines_filter`
  resolves "comment on source line N" to the diff position the API needs.

The diff tools are **token-safe by construction**: on a large MR they return a
bounded page and list the rest under `omitted_files` / `truncated` instead of
failing. Noise (lockfiles, generated, binary) is listed but not expanded.

**Review against the whole codebase (local clone):**

- **`dependency_impact(symbol, root)`** — everywhere a symbol is used (blast
  radius), classified by usage type (ripgrep + tree-sitter AST).
- **`trace_call_chain(symbol, root)`** — the caller/callee graph of a symbol.
- **`mr_review_context(url, root)`** — the north star: per changed symbol, its
  structural impact (always) plus semantically similar code (when the optional
  layer is enabled). Graph-first — structure alone is a valid answer. It also
  **guards the checkout**: graph tools analyze whatever is at `root`, so it warns
  when `root` doesn't hold the MR's head (e.g. left on the target branch) — else
  results silently reflect code without the MR's changes.
- **`myopic worktree <url> <repo>`** — checks out the MR head in a throwaway git
  worktree so the graph tools review the MR's actual code, leaving your main
  checkout untouched.

**Optional semantic layer** (`myopic[semantic]` — lean: lancedb + httpx, no torch):
`index_repo`, `code_search`, and the semantic half of `mr_review_context`. Local
Ollama embeddings (code-specialized model) + embedded LanceDB hybrid search.
Indexing is **incremental** — after the first build, only files whose content
changed are re-embedded — and **freshness-aware**: `index_status(url)` reports
whether the index is fresh, stale (with commits-behind), or built on a different
model, keyed to the git commit it was indexed from. `myopic index <repo>` is a
cron-friendly CLI hook (a stdio server has no place for a scheduler).

**Changed-symbol selection is AST-based:** `mr_review_context` keys off the real
changed *declarations* (the same resolution `mr_diff_sections` uses), not an
identifier-frequency count — so `dependency_impact` runs on actual functions and
classes, never stopwords like `the` / `number` / `styles`.

**Both platforms:** GitLab merge requests **and GitHub pull requests** — pass
either URL and the right backend is chosen automatically. This is what the
platform-abstraction seam (`Review` interface) was built for: GitHub was a new
backend (`GitHubPlatform` + `GitHubReview`), not a rewrite of the tools.

**Close the review loop:**

- **`mr_verify_review(url)`** — read-only. Pairs every existing review thread with
  the diff changes near its commented line, so a re-review after follow-up commits
  is one call instead of re-reading the whole diff. Platform-neutral (works on
  GitHub too, though GitHub doesn't expose thread resolution over REST).
- **`mr_post_comments(url, comments[])`** — the one mutating tool. Posts inline
  comments one at a time from a queue, each immediately visible (no drafts, no
  bulk-publish), retrying transient failures (HTTP 429/5xx) with exponential
  backoff so partial progress survives and rate limits are respected. Writes are
  explicit and clearly separated from the read-only tools.

**Setup:** `pipx install "myopic[semantic]"`; token via `myopic init` or inline in
the client config's `env` block; `myopic doctor` health-checks the platform +
semantic layer and offers to pull the model; `MYOPIC_AUTO_PULL=1` pulls a missing
embedding model on first use.

---

## 🔜 Next

### Auto-refresh the index at review time
`mr_review_context` / `code_search` already surface `index_status`, and the agent
is told to offer a refresh when stale. A future option: an opt-in flag to
transparently run the (incremental, cheap) re-index before answering, so a stale
index never silently degrades a review. **Ideas welcome.**

---

## 🔭 Later

### Learn from review history (`mr_history`)
Mine the last N months of **merged** MRs/PRs — the review threads paired with the
fixes they produced — to learn a codebase's *recurring* review findings (what this
team actually keeps flagging), and use them to sharpen future reviews.

- **Two delivery paths.** (A) Distil the history into a **house checklist** fed to
  the reviewer/agent — high ROI, interpretable, works on the **base install** (no
  embeddings). (B) A **RAG** layer that retrieves similar past findings for the
  current diff — the `myopic[semantic]` version, later, only if (A) proves the
  signal. Start with (A).
- **Cached + incremental, not re-fetched.** History is append-mostly (merged MRs
  are frozen), so cache it and top up only with MRs merged since a stored cursor
  (`merged_at` / IID). Same "index once, delta-update, track freshness" model as
  the code index — with a `history_status` companion.
- **Keyed by the remote repo**, not a local checkout — history comes from the API,
  not the filesystem (a new store, distinct from the code index).
- **Design seam:** myopic provides the corpus (`mr_history` = cached merged-MR
  review threads + fix diffs); the agent/subagent does the distillation. Open
  scoping decisions: cache format (SQLite vs JSONL), and what counts as a
  "substantive" thread worth keeping. Bigger build than a point release — spec first.

### GitHub review-thread resolution
Inline PR comments are surfaced today, but GitHub only exposes thread-resolution
status via GraphQL — so `resolved` is reported as `false` for GitHub. Wiring the
GraphQL call would make it accurate.

### Other ideas on the table
- CI/pipeline status + failed-job logs alongside the review.
- Configurable review "lenses" (security-only, performance-only, conventions-only).

---

## Design principles

1. **Tools return data, not opinions.** myopic gathers context; the agent reviews.
2. **Read and write are separate and obvious.** No tool silently mutates.
3. **Setup friction approaches zero.** If a feature needs heavy local infra, that
   friction is itself a design problem to solve, not a given.
4. **Platform-neutral core.** Diffs and reviews are abstracted; GitLab is just the
   first backend.

Have a better idea for any of these? Open an issue.
