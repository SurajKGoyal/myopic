# myopic roadmap

myopic is built in the open and shipped in small increments. This file is the
honest source of truth for what works, what's next, and why. If something here
interests you, open an issue or a PR — design discussion is welcome.

The north star: **review a change against the whole codebase, not just the
diff.** Everything below ladders up to that.

---

## ✅ Shipped (alpha)

### `mr_review_status(url)`
One snapshot of where a review stands: MR metadata, every discussion thread,
which are resolved vs open, general comments, and a lightweight file-change
summary. Replaces 5-6 separate GitLab API calls.

### `mr_diff_lines(url, files_filter?, lines_filter?)`
The merge request diff as structured, line-numbered hunks — every line tagged
add/del/context with exact old and new line numbers. This is the foundation for
precise inline comments: `lines_filter` translates "comment on source line N"
into the exact diff position the API needs. `files_filter` keeps large reviews
under token limits.

### Platform-abstraction seam
All tools talk to a normalized `Review` interface, never to a platform SDK
directly. GitLab is the first implementation; GitHub is a new implementation,
not a rewrite (see below).

---

## 🔜 Next

### `mr_diff_sections(url)` — AST-grouped diffs
Group changed lines by the function/class they belong to (via tree-sitter) so a
1,000-line MR can be reviewed structurally instead of overflowing the context
window. All changed lines preserved; context optional.

### `review_with_context(url, repo_path)` — the actual point
This is myopic's reason to exist. For each changed symbol, surface:
- **Callers** of modified public symbols (what might break).
- **Conventions** sibling/similar files follow that this change drops
  (missing imports, hooks, error handling).
- **Intra-MR duplication** — near-identical new files copy-pasted within the MR.
- **Unwired files** — new files nothing imports or references (dead on arrival).

Pure structured context, no LLM judgment — the calling agent reviews; myopic
makes sure it isn't reviewing blind. This depends on a codebase index; the
indexing approach (and how to keep setup friction near zero) is the main open
design question. **Ideas welcome.**

### `mr_post_comments(url, comments[])` — explicit, opt-in writes
Bulk-post inline review comments at exact diff positions (resolved via
`mr_diff_lines`), rate-limited for self-hosted GitLab. Writes will always be
explicit and clearly separated from the read-only tools.

---

## 🔭 Later

### GitHub pull requests
The platform layer is already abstracted (`myopic/platforms/base.py`). GitHub
support is a `GitHubPlatform` + `GitHubReview` implementing the same interface —
the tools don't change. This is why the project isn't named `gitlab-*`.

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
