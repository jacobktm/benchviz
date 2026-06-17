"""Run insights rebuilds out-of-process so the web service stays responsive."""

from __future__ import annotations

import os
import subprocess

from app._util import project_root


def rebuild_script_path() -> str:
    return os.path.join(project_root(), "scripts", "rebuild_insights.sh")


def schedule_insights_rebuild(*, wait: bool = False, full: bool = False) -> bool:
    """
    Spawn scripts/rebuild_insights.sh with low CPU priority.

    Returns True when the subprocess was started (or finished when wait=True).
    Returns False when the helper script is missing (caller may fall back).
    """
    script = rebuild_script_path()
    if not os.path.isfile(script):
        return False

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    if full:
        env["BENCHVIZ_INSIGHTS_REBUILD_FULL"] = "1"

    cmd = ["nice", "-n", "10", "bash", script]
    cwd = project_root()
    if wait:
        subprocess.run(cmd, cwd=cwd, env=env, check=False)
        return True

    subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True
