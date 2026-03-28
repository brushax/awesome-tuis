#!/usr/bin/env python3
"""Update GitHub repository star counts in README markdown list items."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
ENTRY_RE = re.compile(r"^(\s*-\s+\[[^\]]+\]\()(?P<url>https?://[^)\s]+)(\).*)$")
GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s#?]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
STAR_SUFFIX_RE = re.compile(r"\s+\(⭐ [0-9,]+\)$")
BATCH_SIZE = 40


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--readme",
        default="README.md",
        help="Path to the markdown file to update (default: README.md)",
    )
    return parser.parse_args()


def resolve_github_token() -> str | None:
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.getenv(key)
        if token:
            return token.strip()

    # gh versions in the wild do not all support `gh auth token`.
    for cmd in (
        ["gh", "auth", "token"],
        ["gh", "auth", "status", "--show-token"],
    ):
        try:
            proc = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            continue

        output = (proc.stdout + "\n" + proc.stderr).strip()
        if not output:
            continue

        if cmd[-1] == "--show-token":
            match = re.search(r"Token:\s+([^\s]+)", output)
            if match:
                return match.group(1).strip()
            continue

        return output

    return None


def chunked(seq: list[tuple[str, str]], size: int) -> list[list[tuple[str, str]]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def extract_repo(url: str) -> tuple[str, str] | None:
    match = GITHUB_REPO_RE.match(url)
    if not match:
        return None

    owner = match.group("owner")
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]

    if not owner or not repo:
        return None
    return owner, repo


def build_graphql_query(batch: list[tuple[str, str]]) -> tuple[str, dict[str, str]]:
    variables: dict[str, str] = {}
    args: list[str] = []
    fields: list[str] = []

    for i, (owner, repo) in enumerate(batch):
        owner_key = f"owner{i}"
        repo_key = f"repo{i}"
        alias = f"r{i}"
        variables[owner_key] = owner
        variables[repo_key] = repo
        args.append(f"${owner_key}: String!, ${repo_key}: String!")
        fields.append(
            f"{alias}: repository(owner: ${owner_key}, name: ${repo_key}) "
            "{ stargazerCount }"
        )

    query = f"query({', '.join(args)}) {{\n  " + "\n  ".join(fields) + "\n}"
    return query, variables


def github_graphql_request(query: str, variables: dict[str, str], token: str) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GITHUB_GRAPHQL_URL,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "awesome-tuis-star-updater",
        },
    )

    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_star_counts(
    repos: list[tuple[str, str]], token: str
) -> tuple[dict[tuple[str, str], int], list[tuple[str, str]]]:
    counts: dict[tuple[str, str], int] = {}
    missing: list[tuple[str, str]] = []

    batches = chunked(repos, BATCH_SIZE)
    for index, batch in enumerate(batches, start=1):
        print(
            f"Fetching stars batch {index}/{len(batches)} ({len(batch)} repos)...",
            file=sys.stderr,
        )
        query, variables = build_graphql_query(batch)
        try:
            payload = github_graphql_request(query, variables, token)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub GraphQL request failed with HTTP {exc.code}: {body}"
            ) from exc

        data = payload.get("data") or {}
        for i, repo in enumerate(batch):
            item = data.get(f"r{i}")
            if item and "stargazerCount" in item:
                counts[repo] = int(item["stargazerCount"])
            else:
                missing.append(repo)

    return counts, missing


def update_lines(lines: list[str], stars: dict[tuple[str, str], int]) -> tuple[list[str], int]:
    changed = 0
    updated_lines = list(lines)

    for i, line in enumerate(lines):
        match = ENTRY_RE.match(line)
        if not match:
            continue

        repo = extract_repo(match.group("url"))
        if not repo:
            continue

        stars_count = stars.get(repo)
        if stars_count is None:
            continue

        base = STAR_SUFFIX_RE.sub("", line.rstrip("\n"))
        next_line = f"{base} (⭐ {stars_count:,})\n"
        if next_line != line:
            updated_lines[i] = next_line
            changed += 1

    return updated_lines, changed


def main() -> int:
    args = parse_args()
    readme_path = Path(args.readme)

    if not readme_path.exists():
        print(f"File not found: {readme_path}", file=sys.stderr)
        return 1

    token = resolve_github_token()
    if not token:
        print(
            "Missing GitHub token. Set GITHUB_TOKEN/GH_TOKEN or run `gh auth login`.",
            file=sys.stderr,
        )
        return 1

    original_lines = readme_path.read_text(encoding="utf-8").splitlines(keepends=True)
    repos: set[tuple[str, str]] = set()
    for line in original_lines:
        match = ENTRY_RE.match(line)
        if not match:
            continue

        repo = extract_repo(match.group("url"))
        if repo:
            repos.add(repo)

    repo_list = sorted(repos)
    if not repo_list:
        print("No GitHub repositories found in markdown list entries.", file=sys.stderr)
        return 0

    print(f"Found {len(repo_list)} GitHub repositories.", file=sys.stderr)
    stars, missing = fetch_star_counts(repo_list, token)
    print(f"Resolved stars for {len(stars)} repositories.", file=sys.stderr)
    if missing:
        print(
            f"Skipped {len(missing)} repositories that could not be resolved.",
            file=sys.stderr,
        )

    updated_lines, changed_count = update_lines(original_lines, stars)
    if changed_count == 0:
        print("README is already up to date.", file=sys.stderr)
        return 0

    readme_path.write_text("".join(updated_lines), encoding="utf-8")
    print(f"Updated {changed_count} line(s) in {readme_path}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
