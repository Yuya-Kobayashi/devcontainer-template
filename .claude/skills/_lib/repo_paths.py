"""Shared path helpers for skills that touch repo-rooted data dirs.

The repo runs a worktree workflow (see AGENTS.md): the main checkout stays on
`main`, and each task — including each agent in a multi-agent run — works in an
isolated tree under `.worktrees/<project>/<slug>/`. That isolation is the point,
but two classes of repo-rooted data want to escape it in opposite directions:

* **Cache** — large, shared, gitignored, re-derivable: anything a skill
  *downloads* or *builds* and would be wasteful to reproduce per worktree
  (datasets, model weights, dependency caches, fetched API/research results).
  One copy belongs to the shared **main checkout** so every worktree and every
  agent reads and writes the same bytes — download once, not once per agent.
  Use `cache_root()`.

* **Corpus** — tracked, committable: the artifacts a run is meant to *produce
  and commit* (generated docs, derived datasets, reports). When a run is driven
  from its own worktree, writing the corpus *into that worktree* lets it be
  committed in place — no copy-into-worktree then revert-main dance.
  Use `corpus_root()`.

The seam between them is the `AGENT_CORPUS_ROOT` environment variable: a
multi-agent orchestrator sets it to the run's worktree before driving the
stages, so every stage's corpus output lands there while its cache reads/writes
still resolve to the shared main checkout. When it is unset (the common case — a
single skill run straight from the main checkout, a test, a manual invocation),
`corpus_root()` == `cache_root()` and behaviour is identical, so this split is a
no-op until a driver opts in.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Env var naming the corpus root (a worktree). Set by a multi-agent orchestrator
#: for a run; unset everywhere else, where the corpus root collapses to the cache
#: root (the main checkout).
CORPUS_ROOT_ENV = "AGENT_CORPUS_ROOT"


def cache_root() -> Path:
    """Return the main (non-worktree) repo root — home of the shared caches.

    Uses `git rev-parse --git-common-dir`, which always points at the main
    checkout's `.git` directory (worktrees share a single common dir).
    `Path(common_dir).parent` is therefore the main repo root, no matter which
    worktree the caller runs in.

    Falls back to walking up from this file's location if `git` isn't on
    PATH or the current tree isn't a git checkout, so the helper is safe
    in stripped-down environments (e.g. CI fixtures).
    """
    try:
        common_dir = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # _lib/ -> skills/ -> .claude/ -> repo root
        return Path(__file__).resolve().parents[3]
    return Path(common_dir).parent


def corpus_root() -> Path:
    """Return the root for tracked, committable artifacts.

    `AGENT_CORPUS_ROOT` (a run's worktree, set by the orchestrator) when present;
    otherwise the main checkout (`cache_root()`), so an un-opted-in caller behaves
    exactly as before.

    If `AGENT_CORPUS_ROOT` is set but does not point at an existing directory it is
    a configuration error: we raise rather than silently fall back to the main
    checkout, because falling back would write the corpus into a dirty main — the
    very thing the split exists to prevent.
    """
    raw = os.environ.get(CORPUS_ROOT_ENV)
    if not raw:
        return cache_root()
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise ValueError(
            f"{CORPUS_ROOT_ENV}={raw!r} does not point at an existing directory; "
            "it must name the run's worktree."
        )
    return root.resolve()
