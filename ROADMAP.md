# myopic roadmap

myopic is built in the open and shipped in small increments. This file is the
honest source of truth for what works, what's next, and why. If something here
interests you, open an issue or a PR — design discussion is welcome.

The north star: **review a change against the whole codebase, not just the
diff.** Everything below ladders up to that.

---

## ✅ Shipped (alpha — v0.0.1)

**Read the merge request — token-safe on any MR size:**

- **`mr_review_status(url)`** — one snapshot of where a review stands: metadata,
  every discussion thread, resolved vs open, general comments, file-change summary.
- **`mr_changed_files(url)`** — a content-free manifest (paths, stats, noise flags)
  that always fits, no matter how large the MR. The cheap entry point.
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
  layer is enabled). Graph-first — structure alone is a valid answer.

**Optional semantic layer** (`myopic[semantic]` — lean: lancedb + httpx, no torch):
`index_repo`, `code_search`, and the semantic half of `mr_review_context`. Local
Ollama embeddings (code-specialized model) + embedded LanceDB hybrid search.

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

**Also:** an interactive `myopic init` setup wizard.

---

## 🔜 Next

### Sharper changed-symbol selection
`mr_review_context` currently picks changed symbols by identifier frequency. Using
the AST section symbols from `mr_diff_sections` would target the actual changed
declarations more precisely. **Ideas welcome.**

---

## 🔭 Later

### GitHub review-thread resolution
Inline PR comments are surfaced today, but GitHub only exposes thread-resolution
status via GraphQL — so `resolved` is reported as `false` for GitHub. Wiring the
GraphQL call would make it accurate.

### Other ideas on the table
- CI/pipeline status + failed-job logs alongside the review.
- Review-thread verification: did a later commit actually address each comment?
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
