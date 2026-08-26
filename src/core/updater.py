"""GitHub-backed update detection and automatic fast-forward sync."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.paths import default_paths
    from core.version import APP_VERSION, PACKAGE_VERSION
else:
    from .paths import default_paths
    from .version import APP_VERSION, PACKAGE_VERSION

from core.i18n import t


DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
UPDATE_CHECK_FILE = "update_check.json"
REMOTE_ENV = "CODEXTOP_UPDATE_REMOTE"
BRANCH_ENV = "CODEXTOP_UPDATE_BRANCH"
GITHUB_URL_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[^/\s:]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)
RELEASE_TAG_RE = re.compile(
    r"^v?(?P<version>\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z_.-]+)?)$"
)


class UpdateError(RuntimeError):
    """Raised when update detection or synchronization cannot continue safely."""


@dataclass(frozen=True)
class GitHubRelease:
    tag_name: str
    version: str
    name: str
    body: str


@dataclass(frozen=True)
class UpdateStatus:
    repo_root: str
    remote: str
    branch: str
    remote_url: str
    github_repo: str | None
    current_version: str
    latest_version: str | None
    release_name: str | None
    release_notes: str | None
    local_revision: str | None
    remote_revision: str | None
    dirty: bool
    update_available: bool
    reason: str


@dataclass(frozen=True)
class UpdateResult:
    status: UpdateStatus
    updated: bool
    before_revision: str | None
    after_revision: str | None
    message: str


def package_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_git(
    args: list[str],
    repo_root: Path,
    *,
    check: bool = True,
    timeout: int = 45,
    stream: bool = False,
) -> str:
    if stream:
        print(f"$ git {' '.join(args)}", flush=True)
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            timeout=timeout,
            check=False,
        )
        if check and proc.returncode != 0:
            raise UpdateError(t("update.git_failed", command=" ".join(args), code=proc.returncode))
        return ""
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise UpdateError(detail or t("update.git_failed", command=" ".join(args), code=proc.returncode))
    return proc.stdout.strip()


def discover_repo_root(start: Path | None = None) -> Path:
    start = (start or package_repo_root()).expanduser().resolve()
    try:
        root = run_git(["rev-parse", "--show-toplevel"], start)
    except UpdateError as exc:
        raise UpdateError(t("update.not_checkout", path=start)) from exc
    return Path(root)


def current_branch(repo_root: Path) -> str:
    branch = run_git(["branch", "--show-current"], repo_root, check=False)
    return branch or DEFAULT_BRANCH


def resolve_remote(repo_root: Path, remote: str | None) -> tuple[str, str]:
    remote_ref = (remote or os.environ.get(REMOTE_ENV) or DEFAULT_REMOTE).strip()
    if not remote_ref:
        remote_ref = DEFAULT_REMOTE
    if re.match(r"^(?:https?://|ssh://|git@)", remote_ref):
        return remote_ref, remote_ref
    remote_path = Path(remote_ref).expanduser()
    if remote_path.exists():
        resolved = str(remote_path.resolve())
        return resolved, resolved
    remote_url = run_git(["remote", "get-url", remote_ref], repo_root)
    return remote_ref, remote_url


def github_repo_from_url(url: str) -> str | None:
    match = GITHUB_URL_RE.match(url.strip())
    if not match:
        return None
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"{match.group('owner')}/{repo}"


def normalize_version(value: str) -> tuple[tuple[int, ...], str]:
    text = value.strip().lstrip("vV")
    main, _, suffix = text.partition("-")
    numbers = tuple(int(part) for part in main.split(".") if part.isdigit())
    return numbers, suffix


def version_is_newer(candidate: str, current: str) -> bool:
    candidate_key = normalize_version(candidate)
    current_key = normalize_version(current)
    width = max(len(candidate_key[0]), len(current_key[0]), 3)
    candidate_numbers = candidate_key[0] + (0,) * (width - len(candidate_key[0]))
    current_numbers = current_key[0] + (0,) * (width - len(current_key[0]))
    return candidate_numbers > current_numbers


def latest_github_release(github_repo: str, *, timeout: int = 15) -> GitHubRelease | None:
    request = Request(
        f"https://api.github.com/repos/{github_repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"CodexTOP/{PACKAGE_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(t("update.release_request_failed", error=exc.reason or exc.code)) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateError(t("update.release_request_failed", error=exc)) from exc

    if not isinstance(payload, dict):
        raise UpdateError(t("update.release_invalid"))
    tag_name = payload.get("tag_name")
    match = RELEASE_TAG_RE.fullmatch(tag_name.strip()) if isinstance(tag_name, str) else None
    if match is None:
        raise UpdateError(t("update.release_tag_invalid", tag=tag_name or "-"))
    if payload.get("draft") or payload.get("prerelease"):
        return None
    name = payload.get("name")
    body = payload.get("body")
    return GitHubRelease(
        tag_name=tag_name,
        version=match.group("version"),
        name=name.strip() if isinstance(name, str) and name.strip() else tag_name,
        body=body if isinstance(body, str) else "",
    )


def remote_branch_revision(repo_root: Path, remote_ref: str, branch: str) -> str | None:
    output = run_git(["ls-remote", remote_ref, f"refs/heads/{branch}"], repo_root, timeout=30)
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
            return parts[0]
    return None


def local_revision(repo_root: Path) -> str | None:
    value = run_git(["rev-parse", "HEAD"], repo_root, check=False)
    return value or None


def has_dirty_worktree(repo_root: Path) -> bool:
    return bool(run_git(["status", "--porcelain"], repo_root, check=False))


def check_update(
    repo_root: Path | None = None,
    *,
    remote: str | None = None,
    branch: str | None = None,
    allow_non_github: bool = False,
) -> UpdateStatus:
    root = discover_repo_root(repo_root)
    remote_ref, remote_url = resolve_remote(root, remote)
    github_repo = github_repo_from_url(remote_url)
    if github_repo is None and not allow_non_github:
        raise UpdateError(t("update.source_not_github", url=remote_url))

    selected_branch = branch or os.environ.get(BRANCH_ENV) or current_branch(root)
    selected_branch = selected_branch.strip() or DEFAULT_BRANCH
    current_rev = local_revision(root)
    remote_rev = remote_branch_revision(root, remote_ref, selected_branch)
    release = latest_github_release(github_repo) if github_repo is not None else None
    latest_version = release.version if release is not None else None
    dirty = has_dirty_worktree(root)

    if remote_rev is None:
        raise UpdateError(t("update.branch_not_found", remote=remote_ref, branch=selected_branch))

    version_update = latest_version is not None and version_is_newer(latest_version, PACKAGE_VERSION)
    update_available = bool(version_update)
    if version_update:
        reason = t("update.remote_version_newer", latest=latest_version, current=APP_VERSION)
    else:
        reason = t("update.up_to_date_reason")

    return UpdateStatus(
        repo_root=str(root),
        remote=remote_ref,
        branch=selected_branch,
        remote_url=remote_url,
        github_repo=github_repo,
        current_version=PACKAGE_VERSION,
        latest_version=latest_version,
        release_name=release.name if release is not None else None,
        release_notes=release.body if release is not None else None,
        local_revision=current_rev,
        remote_revision=remote_rev,
        dirty=dirty,
        update_available=update_available,
        reason=reason,
    )


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def apply_update(
    repo_root: Path | None = None,
    *,
    remote: str | None = None,
    branch: str | None = None,
    allow_dirty: bool = False,
    allow_non_github: bool = False,
    stream: bool = False,
) -> UpdateResult:
    status = check_update(repo_root, remote=remote, branch=branch, allow_non_github=allow_non_github)
    root = Path(status.repo_root)
    before = status.local_revision
    if not status.update_available:
        return UpdateResult(status, False, before, before, t("update.already_current"))
    if status.dirty and not allow_dirty:
        raise UpdateError(t("update.dirty_refused"))

    fetch_args = ["fetch", "--progress", status.remote, status.branch] if stream else [
        "fetch",
        "--quiet",
        status.remote,
        status.branch,
    ]
    run_git(fetch_args, root, timeout=90, stream=stream)
    fetched = run_git(["rev-parse", "FETCH_HEAD"], root)
    current = run_git(["rev-parse", "HEAD"], root)
    if current == fetched:
        return UpdateResult(status, False, before, current, t("update.current_after_fetch"))
    if not is_ancestor(root, current, fetched):
        raise UpdateError(t("update.not_fast_forward"))
    run_git(["merge", "--ff-only", "--stat", "FETCH_HEAD"], root, timeout=90, stream=stream)
    after = run_git(["rev-parse", "HEAD"], root)
    return UpdateResult(status, True, before, after, t("update.updated_to", revision=after[:12]))


def short_rev(value: str | None) -> str:
    return value[:12] if value else "-"


def status_lines(status: UpdateStatus) -> list[str]:
    lines = [
        f"CodexTOP {APP_VERSION}",
        f"GitHub: {status.github_repo or '-'}",
        f"{t('update.label.branch')}: {status.branch}",
        f"{t('update.label.local')}:  {short_rev(status.local_revision)}",
        f"{t('update.label.remote')}: {short_rev(status.remote_revision)}",
        f"{t('update.label.latest_version')}: {('v' + status.latest_version) if status.latest_version else '-'}",
        f"{t('update.label.dirty')}: {t('common.yes') if status.dirty else t('common.no')}",
        f"{t('update.label.update')}: {t('common.available') if status.update_available else t('common.none')}",
        f"{t('update.label.reason')}: {status.reason}",
    ]
    return lines


def result_payload(result: UpdateResult) -> dict[str, Any]:
    return {
        "updated": result.updated,
        "before_revision": result.before_revision,
        "after_revision": result.after_revision,
        "message": result.message,
        "status": asdict(result.status),
    }


def clear_daily_update_cache() -> None:
    try:
        (default_paths().settings_dir / UPDATE_CHECK_FILE).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=t("update.arg.description"))
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=t("update.arg.repo_root"),
    )
    parser.add_argument(
        "--remote",
        default=None,
        help=t("update.arg.remote", env=REMOTE_ENV),
    )
    parser.add_argument("--branch", default=None, help=t("update.arg.branch", env=BRANCH_ENV))
    parser.add_argument("--check-only", action="store_true", help=t("update.arg.check_only"))
    parser.add_argument("--allow-dirty", action="store_true", help=t("update.arg.allow_dirty"))
    parser.add_argument("--allow-non-github", action="store_true", help=t("update.arg.allow_non_github"))
    parser.add_argument("--json", action="store_true", help=t("update.arg.json"))
    args = parser.parse_args()

    try:
        if args.check_only:
            status = check_update(
                args.repo_root,
                remote=args.remote,
                branch=args.branch,
                allow_non_github=args.allow_non_github,
            )
            if args.json:
                print(json.dumps(asdict(status), ensure_ascii=False, indent=2))
            else:
                print("\n".join(status_lines(status)))
            return 0

        result = apply_update(
            args.repo_root,
            remote=args.remote,
            branch=args.branch,
            allow_dirty=args.allow_dirty,
            allow_non_github=args.allow_non_github,
            stream=not args.json,
        )
        clear_daily_update_cache()
        if args.json:
            print(json.dumps(result_payload(result), ensure_ascii=False, indent=2))
        else:
            print("\n".join(status_lines(result.status)))
            print(result.message)
        return 0
    except UpdateError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(t("update.failed", error=exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
