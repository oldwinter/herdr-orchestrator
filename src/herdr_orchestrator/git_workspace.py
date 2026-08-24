from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
        runner: GitRunner = subprocess.run,
    ) -> None:
        self.repository = repository.resolve()
        self.runtime_root = runtime_root.resolve()
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
        status = self._git(worktree.path, "status", "--porcelain").stdout
        if status.strip():
            raise GitWorkspaceError(f"worktree_dirty: {worktree.path}")
        commit = self._git(worktree.path, "rev-parse", "HEAD").stdout.strip()
        if commit == worktree.base_commit:
            raise GitWorkspaceError(f"worktree_commit_missing: {worktree.branch}")
        return commit

    def merge(self, integration: Worktree, ticket: Worktree) -> str:
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
        return self._git(worktree.path, "rev-parse", "HEAD").stdout.strip()

    def _create(self, path: Path, branch: str, base_commit: str) -> Worktree:
        if not BRANCH.fullmatch(branch):
            raise GitWorkspaceError("worktree_branch_invalid")
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
