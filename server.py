#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=2.2.0,<3",
#     "python-bareos>=24.0.0",
# ]
# ///
"""A minimalistic MCP server for Bareos (stdio transport).

Talks to the Bareos Director's native Console protocol directly, in JSON API mode (`.api json`),
via the official `python-bareos` client library — never shells out to `bconsole`, never parses
interactive text output. This is the whole server: one file, no build step, no compiled binary.
`uv` resolves the two dependencies (`mcp`, `python-bareos`) into an ephemeral venv on first run.

Run directly (executable, PEP 723 shebang):

    ./server.py

Configuration is entirely through environment variables — there are **no defaults**, deliberately:
this file is meant to be usable against anyone's Bareos deployment, so nothing about *your*
Director should be baked in.

    BAREOS_DIRECTOR_HOST      required — Director address
    BAREOS_DIRECTOR_PORT      required — Director Console port (Bareos' own default is 9101)
    BAREOS_CONSOLE_NAME       required — a named Console configured on the Director for this server
    BAREOS_CONSOLE_PASSWORD   required — that Console's password

See README.md for how to set up the Director-side Console/Profile this expects.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import bareos.bsock
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"{name} is required (see server.py's module docstring / README.md)", file=sys.stderr)
        sys.exit(1)
    return value


DIRECTOR_HOST = _require_env("BAREOS_DIRECTOR_HOST")
DIRECTOR_PORT = int(_require_env("BAREOS_DIRECTOR_PORT"))
CONSOLE_NAME = _require_env("BAREOS_CONSOLE_NAME")
CONSOLE_PASSWORD = _require_env("BAREOS_CONSOLE_PASSWORD")


def _call(command: str) -> Any:
    """Open a fresh Director connection, run one console command in JSON API mode, close it."""
    director = bareos.bsock.DirectorConsoleJson(
        address=DIRECTOR_HOST,
        port=DIRECTOR_PORT,
        name=CONSOLE_NAME,
        password=bareos.bsock.Password(CONSOLE_PASSWORD),
    )
    try:
        return director.call(command)
    finally:
        director.close()


READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

mcp = MCPServer("bareos")


# --- Read-only tools ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def status(scope: str = "director", name: str = "") -> Any:
    """Bareos status. scope: 'director' (default), 'storage', or 'schedule'.
    name: Storage resource name, required when scope='storage'."""
    if scope == "director":
        return _call("status director")
    if scope == "storage":
        if not name:
            raise ValueError("name is required when scope='storage'")
        return _call(f"status storage={name}")
    if scope == "schedule":
        return _call("status schedule")
    raise ValueError("scope must be 'director', 'storage', or 'schedule'")


@mcp.tool(annotations=READ_ONLY)
def list_jobs(client: str = "", jobstatus: str = "", limit: int = 50) -> Any:
    """List recent Bareos jobs, most recent first. Optionally filter by client name and/or
    jobstatus (Bareos single-letter codes, e.g. 'E' = error, 'T' = terminated normally,
    'R' = running)."""
    cmd = "list jobs"
    if client:
        cmd += f" client={client}"
    if jobstatus:
        cmd += f" jobstatus={jobstatus}"
    cmd += f" limit={limit}"
    return _call(cmd)


@mcp.tool(annotations=READ_ONLY)
def job_details(jobid: int) -> Any:
    """Full detail on one job: files/bytes, pool, storage, start/end time, status."""
    return _call(f"llist jobid={jobid}")


@mcp.tool(annotations=READ_ONLY)
def list_clients() -> Any:
    """List all configured Bareos clients (file daemons)."""
    return _call(".clients")


@mcp.tool(annotations=READ_ONLY)
def list_pools() -> Any:
    """List all configured Bareos pools."""
    return _call(".pools")


@mcp.tool(annotations=READ_ONLY)
def list_storages() -> Any:
    """List all configured Bareos storage resources."""
    return _call(".storages")


@mcp.tool(annotations=READ_ONLY)
def list_filesets() -> Any:
    """List all configured Bareos filesets."""
    return _call(".filesets")


@mcp.tool(annotations=READ_ONLY)
def list_schedules() -> Any:
    """List all configured Bareos schedules."""
    return _call(".schedule")


@mcp.tool(annotations=READ_ONLY)
def list_volumes(pool: str = "") -> Any:
    """List Bareos volumes, optionally filtered by pool name."""
    cmd = "list volumes"
    if pool:
        cmd += f" pool={pool}"
    return _call(cmd)


@mcp.tool(annotations=READ_ONLY)
def volume_details(volume: str) -> Any:
    """Full detail on one volume: status, jobs written to it, bytes, expiry."""
    return _call(f"llist volume={volume}")


@mcp.tool(annotations=READ_ONLY)
def messages() -> Any:
    """Recent director messages (same feed as bconsole's 'messages' command)."""
    return _call("messages")


# --- Mutating tools — all require confirm=True on a second call ------------------------------
#
# First call (confirm=False, the default) never touches the Director for these — it just echoes
# back what would run, so a caller (human or agent) can review before committing.


@mcp.tool(annotations=DESTRUCTIVE)
def run_job(job: str, level: str = "", client: str = "", confirm: bool = False) -> Any:
    """Trigger a Bareos job. Call once with confirm=False (default) to preview the command that
    would run; call again with confirm=True to actually run it. level e.g. 'Full',
    'Incremental', 'Differential'."""
    parts = [f"job={job}"]
    if level:
        parts.append(f"level={level}")
    if client:
        parts.append(f"client={client}")
    command = "run " + " ".join(parts)
    if not confirm:
        return {"preview": True, "would_run": command, "note": "call again with confirm=True to execute"}
    return _call(command + " yes")


@mcp.tool(annotations=DESTRUCTIVE)
def cancel_job(jobid: int, confirm: bool = False) -> Any:
    """Cancel a running Bareos job. Call once with confirm=False (default) to preview; call
    again with confirm=True to actually cancel."""
    if not confirm:
        return {"preview": True, "would_cancel_jobid": jobid, "note": "call again with confirm=True to execute"}
    return _call(f"cancel jobid={jobid} yes")


# --- Restore — exposed as the raw .bvfs_* building blocks bareos-webui's restore page uses,
# rather than one auto-piloted tool. Bareos' bvfs restore-selection flow (get_jobids -> update ->
# lsdirs/lsfiles to browse -> bvfs_restore to build a selection -> restore to launch it) is
# multi-step and stateful on the Director side; composing it one inspectable step at a time here
# is safer than guessing the whole chain in one call for something that can write to disk on a
# target host. See docs.bareos.org/DeveloperGuide/pythonapi.html#bvfs and bconsole's `restore`
# command for the underlying semantics if a step's output shape is unclear.


@mcp.tool(annotations=READ_ONLY)
def bvfs_get_jobids(client: str, jobid: int = 0) -> Any:
    """Resolve the job ids that make up client's most recent restorable backup set (latest full
    plus subsequent differential/incrementals). Pass jobid to instead resolve the set as of that
    specific job. First step of a restore."""
    cmd = f".bvfs_get_jobids client={client}"
    if jobid:
        cmd += f" jobid={jobid}"
    return _call(cmd)


@mcp.tool(annotations=READ_ONLY)
def bvfs_update(jobid: str) -> Any:
    """Populate the Director's bvfs browse cache for the given job id(s) (comma-separated, from
    bvfs_get_jobids) — required once before bvfs_lsdirs/bvfs_lsfiles/bvfs_restore will see them."""
    return _call(f".bvfs_update jobid={jobid}")


@mcp.tool(annotations=READ_ONLY)
def bvfs_lsdirs(jobid: str, path: str = "") -> Any:
    """List subdirectories of path (bvfs path, "" = root) as of the given job id(s). Use to
    browse toward what you want to restore."""
    return _call(f'.bvfs_lsdirs jobid={jobid} path="{path}"')


@mcp.tool(annotations=READ_ONLY)
def bvfs_lsfiles(jobid: str, path: str = "") -> Any:
    """List files directly inside path (bvfs path, "" = root) as of the given job id(s)."""
    return _call(f'.bvfs_lsfiles jobid={jobid} path="{path}"')


@mcp.tool(annotations=READ_ONLY)
def bvfs_restore(jobid: str, path: str = "", fileid: str = "", dirid: str = "") -> Any:
    """Build a restore selection on the Director from bvfs ids gathered via bvfs_lsdirs/
    bvfs_lsfiles (dirid to select whole directories recursively, fileid for individual files —
    comma-separated lists of the 'pathid'/'fileid' values from those tools), or path for a whole
    subtree by bvfs path. Returns how to reference the selection in restore_job's `file` argument
    (see the Director's response — typically a '?b2<jobid>'-style selection name)."""
    cmd = f".bvfs_restore jobid={jobid}"
    if path:
        cmd += f' path="{path}"'
    if fileid:
        cmd += f" fileid={fileid}"
    if dirid:
        cmd += f" dirid={dirid}"
    return _call(cmd)


@mcp.tool(annotations=DESTRUCTIVE)
def restore_job(
    file_selection: str,
    client: str,
    restore_client: str = "",
    where: str = "/tmp/bareos-restore",
    storage: str = "",
    confirm: bool = False,
) -> Any:
    """Launch a restore job for a selection built with bvfs_restore. `file_selection` is the
    selection reference bvfs_restore's response indicates (e.g. '?b2<jobid>'). Restores into
    `restore_client` (defaults to `client`) under `where` — never restores in place unless you
    explicitly pass where="". Pass storage=<name> to target a specific Storage resource (e.g. one
    your Director reserves for restore/verify jobs, if it has one) instead of the catalog's
    default. Call once with confirm=False (default) to preview; call again with confirm=True to
    actually launch it."""
    parts = [
        f"file={file_selection}",
        f"client={client}",
        f"restoreclient={restore_client or client}",
        f"where={where}",
    ]
    if storage:
        parts.append(f"storage={storage}")
    command = "restore " + " ".join(parts)
    if not confirm:
        return {"preview": True, "would_run": command, "note": "call again with confirm=True to execute"}
    return _call(command + " yes")


if __name__ == "__main__":
    mcp.run()
