#!/usr/bin/env python3
"""Build the branch of the plugins repository that Claude's plugin directory reads.

The plugins repository packages AdLoop Cloud for several assistants in one
tree. Claude's plugin directory accepts only a short list of file types, and
the Cursor rule (``.mdc``) and the vector icon (``.svg``) are not on it. So the
directory follows the ``claude`` branch, which holds the Claude plugin alone:

  - ``.claude-plugin/plugin.json`` and ``.mcp.json``
  - ``skills/`` (the orchestration rules)
  - ``assets/adloop-icon.png``, ``LICENSE``
  - ``README.md``, with the parts about the other assistants taken out

Run it after the release commit on the plugins repository's ``main``:

    python3 scripts/plugins/build_claude_branch.py           # commit to the local branch
    python3 scripts/plugins/build_claude_branch.py --check   # exit 1 if the branch is behind
    git -C ../adloop-plugins push origin claude

The branch is built from what is committed on ``main``, with git plumbing: no
checkout, and the working tree is not touched. Nothing is committed when the
branch already holds the same files. Every new commit on the branch is a new
version for the directory and replaces a version that is waiting for review,
so move the branch on purpose. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Lives in the adloop repo; works on the sibling adloop-plugins checkout
# (override with ADLOOP_PLUGINS_DIR), which ships no code of its own.
ADLOOP_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(
    os.environ.get("ADLOOP_PLUGINS_DIR", ADLOOP_ROOT.parent / "adloop-plugins")
).resolve()

SOURCE_BRANCH = "main"
TARGET_BRANCH = "claude"
MANIFEST = ".claude-plugin/plugin.json"
README = "README.md"

# What the Claude plugin consists of. A path ending in "/" takes the folder.
INCLUDED = [
    MANIFEST,
    ".mcp.json",
    "LICENSE",
    README,
    "assets/adloop-icon.png",
    "skills/",
]

# File types the directory accepts from a publisher that is not Verified.
ALLOWED_SUFFIXES = {
    ".md", ".txt", ".json", ".yaml", ".yml",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
}
ALLOWED_NAMES = {"LICENSE"}

# README sections about the other assistants, matched by their heading line.
# Each runs until the next heading of the same or a higher level.
README_DROPPED_SECTIONS = [
    "### Cursor",
    "### Gemini CLI",
    "### ChatGPT and Codex",
]
README_REPLACED_SECTIONS = {
    "## What's in this repository": (
        "## About this branch\n"
        "\n"
        "This branch holds the Claude plugin only: the manifest, the MCP server "
        "entry and the skill with AdLoop's orchestration rules. It is built from "
        "the [main branch](https://github.com/kLOsk/adloop-plugins), which also "
        "packages AdLoop Cloud for Cursor, Gemini CLI, and ChatGPT and Codex.\n"
    ),
}


class BuildError(Exception):
    pass


def git(*args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve(ref: str) -> str | None:
    try:
        return git("rev-parse", "--verify", "--quiet", ref)
    except BuildError:
        return None


def heading_level(line: str) -> int:
    stripped = line.lstrip("#")
    hashes = len(line) - len(stripped)
    return hashes if hashes and stripped.startswith(" ") else 0


def rewrite_sections(text: str) -> str:
    """Drop or replace whole README sections, failing when one is missing."""
    lines = text.split("\n")
    wanted = {heading: None for heading in README_DROPPED_SECTIONS}
    wanted.update(README_REPLACED_SECTIONS)
    seen: set[str] = set()
    out: list[str] = []
    skipping = 0  # heading level of the section being left out
    in_code = False
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
        level = 0 if in_code else heading_level(line)
        if level and skipping and level <= skipping:
            skipping = 0
        if level and line.strip() in wanted:
            seen.add(line.strip())
            skipping = level
            replacement = wanted[line.strip()]
            if replacement is not None:
                out.extend(replacement.split("\n"))
            continue
        if not skipping:
            out.append(line)
    missing = sorted(set(wanted) - seen)
    if missing:
        raise BuildError(
            "README.md no longer has these sections: "
            + ", ".join(missing)
            + f". Update {Path(__file__).name}."
        )
    return "\n".join(out)


def selected_entries(source: str) -> list[tuple[str, str, str]]:
    """(mode, blob, path) for every file of the Claude plugin on the source."""
    entries = []
    found: set[str] = set()
    for line in git("ls-tree", "-r", source).splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, blob = meta.split()
        for wanted in INCLUDED:
            if path == wanted or (wanted.endswith("/") and path.startswith(wanted)):
                if kind != "blob" or mode not in ("100644", "100755"):
                    raise BuildError(f"{path}: not a regular file")
                entries.append((mode, blob, path))
                found.add(wanted)
    missing = sorted(set(INCLUDED) - found)
    if missing:
        raise BuildError(f"missing on {SOURCE_BRANCH}: {', '.join(missing)}")
    for _, _, path in entries:
        name = Path(path).name
        if name not in ALLOWED_NAMES and Path(path).suffix.lower() not in ALLOWED_SUFFIXES:
            raise BuildError(f"{path}: a file type the directory does not accept")
    return entries


def build_tree(source: str) -> tuple[str, str]:
    """Write the branch's tree to the object store; return (tree, version)."""
    entries = selected_entries(source)
    readme = rewrite_sections(git("show", f"{source}:{README}")) + "\n"
    readme_blob = git("hash-object", "-w", "--stdin", stdin=readme)
    version = json.loads(git("show", f"{source}:{MANIFEST}")).get("version")
    if not version:
        raise BuildError(f"{MANIFEST} has no version")

    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git("read-tree", "--empty", env=env)
        for mode, blob, path in entries:
            blob = readme_blob if path == README else blob
            git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
        return git("write-tree", env=env), version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help=f"Don't commit; exit 1 if the {TARGET_BRANCH} branch is behind {SOURCE_BRANCH}",
    )
    parser.add_argument(
        "--trailer",
        action="append",
        default=[],
        help="A trailer line for the commit message (repeatable)",
    )
    args = parser.parse_args()

    try:
        source = resolve(f"refs/heads/{SOURCE_BRANCH}")
        if source is None:
            raise BuildError(f"{REPO_ROOT} has no {SOURCE_BRANCH} branch")
        tree, version = build_tree(source)
        parent = resolve(f"refs/heads/{TARGET_BRANCH}")
        if parent is None:
            # A branch that only exists on the remote so far (fresh clone).
            parent = resolve(f"refs/remotes/origin/{TARGET_BRANCH}")
        current = git("rev-parse", f"{parent}^{{tree}}") if parent else None

        if current == tree:
            print(f"{TARGET_BRANCH} is up to date with {SOURCE_BRANCH} ({version})")
            return 0
        if args.check:
            print(
                f"{TARGET_BRANCH} is behind {SOURCE_BRANCH} ({version}): "
                f"run scripts/plugins/{Path(__file__).name}"
            )
            return 1

        message = f"Claude plugin {version}\n\nBuilt from {SOURCE_BRANCH} {source[:7]}.\n"
        if args.trailer:
            message += "\n" + "\n".join(args.trailer) + "\n"
        commit_args = ["commit-tree", tree, "-m", message]
        if parent:
            commit_args += ["-p", parent]
        commit = git(*commit_args)
        git("update-ref", f"refs/heads/{TARGET_BRANCH}", commit)
    except BuildError as error:
        print(f"Build failed: {error}")
        return 1

    print(f"{TARGET_BRANCH} -> {commit[:7]} (Claude plugin {version}, from {source[:7]})")
    print(f"push with: git -C {REPO_ROOT} push origin {TARGET_BRANCH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
