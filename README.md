# minimize

A TUI for managing [lean-minimizer](https://github.com/kim-em/lean-minimizer) jobs.

Automates workspace setup and provides a terminal dashboard for tracking
long-running Lean 4 code minimization jobs.

## Install

```bash
uv tool install git+https://github.com/leanprover-community/minimize
```

### Requirements

- Python 3.11+
- [tmux](https://github.com/tmux/tmux) (for persistent background jobs)
- [elan](https://github.com/leanprover/elan) + [Lake](https://github.com/leanprover/lean4) (for building Lean projects)

Check your environment with:

```bash
minimize doctor
```

## Usage

### Start a job

Point `minimize run` at any `.lean` file in a Lean project. The file must
contain a `#guard_msgs` block marking the behavior to preserve.

```bash
minimize run path/to/MyFile.lean
minimize run path/to/MyFile.lean --note "investigating issue #1234"
```

This will:
1. Detect the Lean project (walks up to find `lakefile.toml`)
2. Create a workspace in `~/.minimize/` with the right dependencies
3. Run `lake exe cache get` if Mathlib is detected
4. Build and start `lake exe minimize` in a persistent tmux session

### Dashboard

Run `minimize` with no arguments to open the TUI:

```bash
minimize
```

| Key     | Action                           |
|---------|----------------------------------|
| Enter   | Open workspace in VS Code        |
| a       | Attach to tmux session           |
| l       | Toggle log pane                  |
| k       | Kill running job                 |
| d       | Delete job and workspace         |
| r       | Resume stopped job               |
| e       | Edit note                        |
| q       | Quit (jobs keep running)         |

Jobs are color-coded: blue = running, green = completed, red = failed.

### CLI commands

```
minimize run <file.lean>     Start a minimization job
minimize list                Show all jobs (non-interactive)
minimize attach <id>         Attach to tmux session
minimize log <id>            Tail the job log
minimize open <id>           Open workspace in VS Code
minimize kill <id>           Kill a running job
minimize resume <id>         Resume with --resume flag
minimize note <id> [text]    View or set a note
minimize result [id]         Print minimized output
minimize clean               Remove old workspaces
minimize doctor              Check environment
```

Job IDs accept unambiguous prefixes (e.g., `minimize log 04a`).

### Options

```
minimize run <file> --marker PATTERN          Custom marker (default: #guard_msgs)
minimize run <file> --force                   Skip #guard_msgs validation
minimize run <file> --cross-toolchain TC      Cross-version minimization
minimize run <file> --mirror-deps             Mirror deps instead of path dependency
minimize run <file> --extra-args "..."        Extra args for lake exe minimize
minimize clean --older-than 7d               Only clean jobs older than N days
```

### Cross-toolchain minimization

To minimize a file that must compile under two different Lean versions:

```bash
minimize run MyFile.lean --cross-toolchain leanprover/lean4:v4.27.0
```

This builds with both the project's toolchain and the cross toolchain before
running the minimizer with `--cross-toolchain`, ensuring the minimized result
works on both versions. The TUI shows the cross toolchain in a dedicated column.

## How it works

For each job, `minimize` creates a standalone Lake project in `~/.minimize/`
that depends on the source project (as a path dependency) and on
[lean-minimizer](https://github.com/kim-em/lean-minimizer). This means all
imports from the original project resolve correctly without duplicating the
Lake configuration.

Jobs run in named tmux sessions (`minimize-<id>`) that persist across
terminal disconnects. The TUI polls job status by reading phase markers
from the log file.

## License

Apache 2.0
