from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ob_cache_sync._paths import (
    LEGACY_LOCAL_CLONE,
    OB_CACHE_GITHUB,
    default_ob_cache_dir,
    default_pts_clone_dir,
    default_pts_user_path,
    live_ob_fetch_enabled,
    pts_executable,
    pts_user_path_override_value,
)


def _git_cmd_detail(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    if len(text) > 2000:
        return text[-2000:]
    return text


def _run(cmd: list[str], cwd: Path | None = None, *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = _git_cmd_detail(proc)
        msg = f"Command {cmd!r} failed with exit status {proc.returncode}"
        if detail:
            msg += f": {detail}"
        raise RuntimeError(msg)
    return proc


def _git_cmd(repo: Path | None, *args: str) -> list[str]:
    cmd = ["git"]
    if repo is not None:
        cmd.extend(["-c", f"safe.directory={repo.resolve()}"])
    cmd.extend(args)
    return cmd


def _fresh_pts_clone(dest: Path, branch: str) -> None:
    if dest.exists():
        try:
            shutil.rmtree(dest)
        except PermissionError as exc:
            raise RuntimeError(
                f"Cannot remove {dest} (permission denied). "
                f"Fix ownership then retry, e.g.: "
                f"sudo chown -R $(stat -c '%U' {dest.parent}) {dest.parent} "
                f"or sudo rm -rf {dest}"
            ) from exc
    _run(_git_cmd(None, "clone", "--depth", "1", "--single-branch", "--branch", branch,
                   OB_CACHE_GITHUB, str(dest)))


def ensure_pts_clone(
    clone_dir: Path | None = None,
    *,
    branch: str = "master",
) -> dict[str, Any]:
    DEFAULT_BRANCH = "master"
    dest = Path(clone_dir or default_pts_clone_dir())
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        "clone_dir": str(dest),
        "branch": branch,
    }

    if (dest / ".git").is_dir():
        try:
            _run(_git_cmd(dest, "fetch", "origin", branch, "--depth", "1"), cwd=dest)
            _run(_git_cmd(dest, "reset", "--hard", "FETCH_HEAD"), cwd=dest)
            meta["action"] = "updated"
        except RuntimeError as exc:
            meta["fetch_error"] = str(exc)
            _fresh_pts_clone(dest, branch)
            meta["action"] = "recloned"
    elif dest.is_dir() and any(dest.iterdir()):
        raise FileExistsError(
            f"{dest} exists but is not a git checkout; remove it or set BENCHVIZ_PTS_CLONE_DIR"
        )
    else:
        _fresh_pts_clone(dest, branch)
        meta["action"] = "cloned"

    meta["has_ob_cache"] = (dest / "ob-cache" / "test-profiles").is_dir()
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def run_pts_openbenchmarking_refresh(clone_dir: Path | None = None) -> dict[str, Any]:
    from app.ob_cache_sync._paths import PTS_UPDATE_TIMEOUT_SEC

    root = Path(clone_dir or default_pts_clone_dir())
    exe = pts_executable(root)
    user_path = default_pts_user_path()
    user_path.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"clone_dir": str(root), "command": f"{exe} openbenchmarking-refresh"}

    if not exe.is_file():
        meta["skipped"] = True
        meta["reason"] = "phoronix-test-suite script not found"
        return meta

    env = os.environ.copy()
    env["PTS_USER_PATH_OVERRIDE"] = pts_user_path_override_value()
    env.setdefault("PTS_SILENT_MODE", "1")

    try:
        proc = subprocess.run(
            [str(exe), "openbenchmarking-refresh"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=PTS_UPDATE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        meta["ok"] = False
        meta["reason"] = f"timed out after {PTS_UPDATE_TIMEOUT_SEC}s"
        meta["stderr"] = (exc.stderr or "")[-4000:] if exc.stderr else ""
        return meta

    meta["ok"] = proc.returncode == 0
    meta["returncode"] = proc.returncode
    if proc.returncode != 0:
        meta["reason"] = f"exit code {proc.returncode}"
        if proc.stderr:
            meta["stderr_tail"] = proc.stderr[-4000:]
    return meta


def run_pts_default_update(clone_dir: Path | None = None) -> dict[str, Any]:
    from app.ob_cache_sync._paths import PTS_UPDATE_TIMEOUT_SEC

    root = Path(clone_dir or default_pts_clone_dir())
    exe = pts_executable(root)
    meta: dict[str, Any] = {"clone_dir": str(root), "command": str(exe)}

    if not exe.is_file():
        meta["skipped"] = True
        meta["reason"] = "phoronix-test-suite script not found"
        return meta

    env = os.environ.copy()
    env.setdefault("PTS_SILENT_MODE", "1")

    try:
        proc = subprocess.run(
            [str(exe)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=PTS_UPDATE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        meta["ok"] = False
        meta["reason"] = f"timed out after {PTS_UPDATE_TIMEOUT_SEC}s"
        meta["stdout"] = (exc.stdout or "")[-4000:] if exc.stdout else ""
        meta["stderr"] = (exc.stderr or "")[-4000:] if exc.stderr else ""
        return meta

    meta["ok"] = proc.returncode == 0
    meta["returncode"] = proc.returncode
    if proc.stdout:
        meta["stdout_tail"] = proc.stdout[-4000:]
    if proc.stderr:
        meta["stderr_tail"] = proc.stderr[-4000:]
    if proc.returncode != 0 and "reason" not in meta:
        meta["reason"] = f"exit code {proc.returncode}"
    meta["ran_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def _resolve_local_clone(local_path: str | Path | None) -> Path:
    if local_path:
        return Path(local_path)
    preferred = default_pts_clone_dir()
    try:
        if preferred.is_dir() and (preferred / "ob-cache").is_dir():
            return preferred
    except PermissionError:
        pass
    legacy = Path(LEGACY_LOCAL_CLONE)
    try:
        if legacy.is_dir() and (legacy / "ob-cache").is_dir():
            return legacy
    except PermissionError:
        pass
    return preferred


def sync_ob_cache(
    dest_dir: Path | None = None,
    *,
    source: str = "auto",
    local_path: str | Path | None = None,
    branch: str = "master",
    ensure_clone: bool = True,
    run_pts_update: bool = False,
    live_fetch: bool = True,
) -> dict[str, Any]:
    from app.ob_cache_sync._lookup import supplement_ob_cache_from_live

    dest = Path(dest_dir or default_ob_cache_dir())
    dest.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"dest": str(dest), "branch": branch}

    use_github_flow = source.lower() in ("auto", "github")
    local = _resolve_local_clone(local_path)

    if ensure_clone and use_github_flow:
        meta["clone"] = ensure_pts_clone(local, branch=branch)
        if run_pts_update:
            meta["pts_update"] = run_pts_openbenchmarking_refresh(local)

    use_local = (
        source.lower() == "local"
        or use_github_flow
        or (source.lower() == "auto" and local.is_dir() and (local / "ob-cache").is_dir())
    )

    if use_local:
        if not local.is_dir() or not (local / "ob-cache").is_dir():
            raise FileNotFoundError(
                f"No ob-cache/ under {local}. Run sync with ensure_clone or install PTS there."
            )
        src_root = local / "ob-cache" / "test-profiles"
        if not src_root.is_dir():
            raise FileNotFoundError(f"No ob-cache/test-profiles under {local}")

    if live_fetch and live_ob_fetch_enabled():
        meta["live_fetch"] = supplement_ob_cache_from_live(dest_dir=dest)

    if use_local:
        copied = _merge_copy_generated_json_files(
            src_root,
            dest / "test-profiles",
            skip_existing=True,
        )
        meta.update({"source": "local", "local_path": str(local), "files_copied": copied})
    else:
        raise ValueError(f"Unsupported source {source!r}; use auto, local, or github")

    meta["synced_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def _merge_copy_generated_json_files(
    src_root: Path,
    dest_root: Path,
    *,
    skip_existing: bool = False,
) -> int:
    count = 0
    if not src_root.is_dir():
        return count
    for src in src_root.rglob("generated.json"):
        rel = src.relative_to(src_root)
        out = dest_root / rel
        if skip_existing and out.is_file():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        count += 1
    return count


def _copy_generated_json_files(src_root: Path, dest_root: Path) -> int:
    if dest_root.exists():
        shutil.rmtree(dest_root)
    return _merge_copy_generated_json_files(src_root, dest_root)
