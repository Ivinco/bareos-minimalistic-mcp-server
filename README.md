# bareos-minimalistic-mcp-server

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)

A minimalistic [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server for
[Bareos](https://www.bareos.org/) — the whole thing is **one Python file**
([`server.py`](server.py)), no build step, no compiled binary, no `bconsole` subprocess.

It talks to the Bareos Director's native Console protocol directly, in JSON API mode
(`.api json`), through the official [`python-bareos`](https://github.com/bareos/python-bareos)
client library — the same protocol `bconsole` and `bareos-webui` use, but as a real structured
API rather than parsing interactive terminal text.

Why this exists: the only other Bareos MCP server at the time of writing,
[edeckers/bareos-mcp-server](https://github.com/edeckers/bareos-mcp-server), is written in Rust
(a compiled binary) and works by shelling out to `bconsole` and scraping its output. This project
takes the opposite approach on both counts.

## Requirements

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — resolves `server.py`'s two
  dependencies (`mcp`, `python-bareos`) into an ephemeral venv on first run. Nothing to install
  ahead of time beyond `uv` itself.
- A Bareos Director you can reach over the network, and a Console credential for it (see below).

## Director-side setup

Add a **named** Console and a Profile to your Director's configuration
(`bareos-dir.d/console/*.conf`, `bareos-dir.d/profile/*.conf`). A named console (rather than the
default one) means exhausting `MaximumConsoleConnections` fails loudly instead of silently
sharing the anonymous connection pool.

```
Profile {
  Name = "mcp"
  # Excludes director-config-editing and catalog-admin commands; allows job control and restore.
  CommandACL = !.bvfs_clear_cache, !.exit, !.sql, !configure, !create, !delete, !purge, !prune, !sqlquery, !umount, !unmount, *all*
  Job ACL = *all*
  Schedule ACL = *all*
  Catalog ACL = *all*
  Pool ACL = *all*
  Storage ACL = *all*
  Client ACL = *all*
  FileSet ACL = *all*
  Where ACL = *all*
  Plugin Options ACL = *all*
}

Console {
  Name = mcp
  Password = "<generate a strong password>"
  Profile = "mcp"
}
```

Narrow the `CommandACL`/`*ACL` lines further if you want a stricter deployment (e.g. drop
`run`/`cancel`/restore-related commands for a read-only-only server). TLS is left at whatever your
Director's default is — `python-bareos` supports TLS-PSK natively, so there's no reason to
disable it for this Console the way some PHP-based tools have to.

## Configuration

Everything is an environment variable. **There are no defaults** — this is deliberate, so the
script never silently points at the wrong Director:

| Variable | Required | Description |
|---|---|---|
| `BAREOS_DIRECTOR_HOST` | yes | Director address |
| `BAREOS_DIRECTOR_PORT` | yes | Director Console port (Bareos' own default is `9101`) |
| `BAREOS_CONSOLE_NAME` | yes | The named Console configured above |
| `BAREOS_CONSOLE_PASSWORD` | yes | That Console's password |

The script exits immediately with a clear message naming the missing variable if any of these
aren't set — it never falls back to guessing.

## Running it

```bash
BAREOS_DIRECTOR_HOST=bareos.example.com \
BAREOS_DIRECTOR_PORT=9101 \
BAREOS_CONSOLE_NAME=mcp \
BAREOS_CONSOLE_PASSWORD=<password> \
./server.py
```

### Connecting an MCP client

**Claude Code:**

```bash
claude mcp add bareos \
  -e BAREOS_DIRECTOR_HOST=bareos.example.com \
  -e BAREOS_DIRECTOR_PORT=9101 \
  -e BAREOS_CONSOLE_NAME=mcp \
  -e BAREOS_CONSOLE_PASSWORD=<password> \
  -- /path/to/server.py
```

**Any `command`-style JSON client config** (Claude Desktop, etc.):

```json
{
  "mcpServers": {
    "bareos": {
      "command": "/path/to/server.py",
      "env": {
        "BAREOS_DIRECTOR_HOST": "bareos.example.com",
        "BAREOS_DIRECTOR_PORT": "9101",
        "BAREOS_CONSOLE_NAME": "mcp",
        "BAREOS_CONSOLE_PASSWORD": "<password>"
      }
    }
  }
}
```

## Tools

Read-only (safe to call freely):

| Tool | Description |
|---|---|
| `status` | Director / storage / schedule status |
| `list_jobs` | Recent job runs, optionally filtered by client/status |
| `job_details` | Full detail on one job |
| `list_clients`, `list_pools`, `list_storages`, `list_filesets`, `list_schedules` | Configured Director resources |
| `list_volumes`, `volume_details` | Volume inventory and detail |
| `messages` | Recent Director messages |
| `bvfs_get_jobids`, `bvfs_update`, `bvfs_lsdirs`, `bvfs_lsfiles`, `bvfs_restore` | The bvfs restore-selection building blocks (see below) |

Mutating (each carries an MCP `destructiveHint` annotation, and requires an explicit second call
to actually execute — see below):

| Tool | Description |
|---|---|
| `run_job` | Trigger a job |
| `cancel_job` | Cancel a running job |
| `restore_job` | Launch a restore for a selection built with `bvfs_restore` |

### The confirm-then-execute pattern

`run_job`, `cancel_job`, and `restore_job` all take a `confirm: bool = False` parameter. Called
without it, they touch nothing — they just echo back the exact Bareos command that *would* run.
Call again with `confirm=True` to actually execute it. This gives a human (or an agent talking to
one) a chance to review the exact command before anything mutates, without relying solely on the
MCP client's own confirmation UI.

### Restore

Bareos' restore selection (`.bvfs_*` commands) is inherently a multi-step, stateful flow on the
Director side: resolve job ids → populate the browse cache → browse directories/files → build a
selection → launch the restore. Rather than one tool that guesses the whole chain, this server
exposes each step as its own read-only tool (`bvfs_get_jobids` → `bvfs_update` →
`bvfs_lsdirs`/`bvfs_lsfiles` → `bvfs_restore`), so a caller can inspect each response before
building the final `restore_job` call. For selective/point-in-time restores beyond a
straightforward whole-tree restore of one client's latest backup, `bconsole`'s interactive
`restore` command remains the more capable tool.

## Design notes

- **One file, PEP 723 inline dependencies.** No `pyproject.toml`, no packaging step — `uv run
  --script` (or the shebang) resolves dependencies into an ephemeral venv on demand. Clone it, run
  it.
- **No credential store integration.** Earlier iterations of this server (internal to Ivinco)
  fetched its Console password from a company-specific secret store. That's out of scope for a
  general-purpose tool — bring your own secret via `BAREOS_CONSOLE_PASSWORD`, from whatever your
  environment already uses (a `.env` file, your shell's secret manager, a container's injected
  env, etc.).
- **No implicit host.** Every deployment of this server should have to say explicitly which
  Director it's talking to.

## License

[MIT](LICENSE) © Ivinco
