from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.delivery_protocol import DeliveryArtifactError, validate_artifact_path

BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")


class GitWorkspaceError(RuntimeError):
    pass


class GitRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


def _subprocess_git_runner(
    argv: list[str],
    *,
    cwd: str,
    capture_output: bool,
    text: bool,
    check: bool,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=capture_output,
        text=text,
        check=check,
        timeout=timeout,
    )


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: str
    base_commit: str


class GitWorkspace:
    def __init__(
        self,
        repository: Path,
        runtime_root: Path,
        slug: str,
        *,
        runner: GitRunner = _subprocess_git_runner,
    ) -> None:
        self.repository = _safe_absolute(repository)
        self.runtime_root = _safe_absolute(runtime_root)
        self.slug = slug
        self.runner = runner

    def base_commit(self) -> str:
        return self._git(self.repository, "rev-parse", "HEAD").stdout.strip()

    def create_integration(self, base_commit: str) -> Worktree:
        return self._create(
            self.runtime_root / "worktrees" / "integration",
            f"ho/{self.slug}/integration",
            base_commit,
        )

    def create_ticket(
        self,
        ticket_id: str,
        *,
        base_commit: str,
    ) -> Worktree:
        return self._create(
            self.runtime_root / "worktrees" / f"ticket-{ticket_id}",
            f"ho/{self.slug}/ticket-{ticket_id}",
            base_commit,
        )

    def validate_commit(self, worktree: Worktree) -> str:
        _validate_worktree_path(worktree.path, self.runtime_root)
        status = self._git(worktree.path, "status", "--porcelain").stdout
        if status.strip():
            raise GitWorkspaceError(f"worktree_dirty: {worktree.path}")
        commit = self._git(worktree.path, "rev-parse", "HEAD").stdout.strip()
        if commit == worktree.base_commit:
            raise GitWorkspaceError(f"worktree_commit_missing: {worktree.branch}")
        if (
            self._git(
                worktree.path,
                "merge-base",
                "--is-ancestor",
                worktree.base_commit,
                commit,
                check=False,
            ).returncode
            != 0
        ):
            raise GitWorkspaceError(f"worktree_commit_diverged: {worktree.branch}")
        return commit

    def merge(self, integration: Worktree, ticket: Worktree) -> str:
        self.validate_commit(ticket)
        _validate_worktree_path(integration.path, self.runtime_root)
        process = self._git(
            integration.path,
            "merge",
            "--no-ff",
            "--no-edit",
            ticket.branch,
            check=False,
        )
        if process.returncode != 0:
            raise GitWorkspaceError(f"ticket_merge_failed: {ticket.branch}")
        return self._git(integration.path, "rev-parse", "HEAD").stdout.strip()

    def head(self, worktree: Worktree) -> str:
        _validate_worktree_path(worktree.path, self.runtime_root)
        return self._git(worktree.path, "rev-parse", "HEAD").stdout.strip()

    def validate_clean(self, path: Path) -> None:
        _validate_worktree_path(path, self.runtime_root)
        if self._git(path, "status", "--porcelain").stdout.strip():
            raise GitWorkspaceError(f"delivery_worktree_dirty: {path}")

    def succeeds(self, cwd: Path, *args: str) -> bool:
        self._validate_git_cwd(cwd)
        return self._git(cwd, *args, check=False).returncode == 0

    def is_ancestor(self, cwd: Path, ancestor: str, descendant: str) -> bool:
        return self.succeeds(cwd, "merge-base", "--is-ancestor", ancestor, descendant)

    def output(self, cwd: Path, *args: str) -> str:
        self._validate_git_cwd(cwd)
        process = self._git(cwd, *args, check=False)
        if process.returncode != 0 or not process.stdout.strip():
            raise GitWorkspaceError("delivery_git_query_failed")
        return process.stdout.strip()

    def validate_ownership(self, expected_path: Path, worktree: Worktree) -> None:
        try:
            expected = _safe_absolute(expected_path)
            actual = _safe_absolute(worktree.path)
            _validate_worktree_path(expected, self.runtime_root)
            _validate_worktree_path(actual, self.runtime_root)
        except GitWorkspaceError as exc:
            raise GitWorkspaceError("delivery_worktree_ownership_invalid") from exc
        if actual != expected or not expected.is_dir():
            raise GitWorkspaceError("delivery_worktree_ownership_invalid")
        repository_common = self._git_path(self.repository, "rev-parse", "--git-common-dir")
        worktree_common = self._git_path(expected, "rev-parse", "--git-common-dir")
        worktree_root = Path(self.output(expected, "rev-parse", "--show-toplevel")).resolve()
        branch = self.output(expected, "branch", "--show-current")
        if (
            worktree_common != repository_common
            or worktree_root != expected
            or branch != worktree.branch
            or self.output(self.repository, "rev-parse", worktree.branch)
            != self.output(expected, "rev-parse", "HEAD")
        ):
            raise GitWorkspaceError("delivery_worktree_ownership_invalid")

    def _git_path(self, cwd: Path, *args: str) -> Path:
        return (cwd / Path(self.output(cwd, *args))).resolve()

    def _validate_git_cwd(self, cwd: Path) -> None:
        candidate = _safe_absolute(cwd)
        if candidate != self.repository and not candidate.is_relative_to(self.runtime_root):
            raise GitWorkspaceError("delivery_git_query_failed")

    def _create(self, path: Path, branch: str, base_commit: str) -> Worktree:
        if not BRANCH.fullmatch(branch):
            raise GitWorkspaceError("worktree_branch_invalid")
        _validate_worktree_path(path, self.runtime_root)
        if path.exists():
            current_branch = self._git(
                path,
                "branch",
                "--show-current",
            ).stdout.strip()
            if current_branch != branch:
                raise GitWorkspaceError(f"worktree_path_conflict: {path}")
            return Worktree(path, branch, base_commit)
        path.parent.mkdir(parents=True, exist_ok=True)
        _validate_worktree_path(path, self.runtime_root)
        process = self._git(
            self.repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            base_commit,
            check=False,
        )
        if process.returncode != 0:
            raise GitWorkspaceError(f"worktree_create_failed: {branch}")
        return Worktree(path, branch, base_commit)

    def _git(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            process = self.runner(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitWorkspaceError("git_command_timeout") from exc
        except OSError as exc:
            raise GitWorkspaceError("git_unavailable") from exc
        if check and process.returncode != 0:
            raise GitWorkspaceError(f"git_command_failed: {' '.join(args)}")
        return process


def _safe_absolute(path: Path) -> Path:
    try:
        return validate_artifact_path(path.expanduser().absolute())
    except DeliveryArtifactError as exc:
        raise GitWorkspaceError("worktree_path_invalid") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise GitWorkspaceError("worktree_path_invalid") from exc


def _validate_worktree_path(path: Path, root: Path) -> None:
    candidate = _safe_absolute(path)
    runtime = _safe_absolute(root)
    if not candidate.is_relative_to(runtime):
        raise GitWorkspaceError("worktree_path_invalid")
