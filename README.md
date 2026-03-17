# slurm-monitor

A live terminal dashboard for monitoring SLURM (sbatch) jobs. Auto-refreshes in real time, shows all running/pending jobs, and lets you scroll through log output — no more manual `squeue` + `tail -f`.

![Python 3.6+](https://img.shields.io/badge/python-3.6%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

![slurm-monitor dashboard](image.png)

## Features

- **Live auto-refresh** — polls `squeue` every 2 seconds (configurable)
- **Color-coded job states** — green for running, yellow for pending, red for failed, etc.
- **Scrollable log viewer** — tails your job's stdout log with full scroll support (auto-detects log file location)
- **GPU utilization panel** — SSHes into allocated nodes to show per-GPU usage and memory via `nvidia-smi`
- **Job detail view** — shows partition, CPUs, GPUs, working directory, and more from `scontrol`
- **Zero dependencies** — uses only Python standard library (`curses`, `subprocess`, `argparse`)

## Requirements

- Python 3.6+
- SLURM cluster with `squeue`, `scontrol` available
- Terminal with curses support (any standard Linux/macOS terminal)
- (Optional) SSH access to compute nodes for GPU monitoring

## Installation

```bash
git clone https://github.com/lihaoxin2020/slurm-monitor.git
cd slurm-monitor
```

No `pip install` needed — it's a single script with no external dependencies.

## Usage

```bash
python3 monitor.py
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `-i`, `--interval` | Refresh interval in seconds | `2` |
| `-u`, `--user` | Monitor a specific user's jobs | current user |

```bash
# Refresh every 5 seconds
python3 monitor.py -i 5

# Monitor another user's jobs
python3 monitor.py -u colleague_name
```

### Keyboard Controls

| Key | Action |
|-----|--------|
| `↑` / `↓` | Select job |
| `d` | Toggle detail panel (job info + log tail) |
| `g` | Toggle GPU utilization panel |
| `j` / `PgDn` | Scroll log down |
| `k` / `PgUp` | Scroll log up |
| `Home` | Jump to top of log |
| `End` | Jump to bottom of log |
| `q` | Quit |

## How it finds log files

The detail panel automatically locates your job's stdout log by:

1. Checking `StdOut` from `scontrol show job`
2. Looking for `slurm-<jobid>.out` in the job's working directory
3. Falling back to `slurm-<jobid>.out` in `$HOME`

## Tips

- Add an alias for quick access:
  ```bash
  alias smon='python3 ~/slurm-monitor/monitor.py'
  ```
- Works great over SSH — just make sure your terminal supports Unicode (most do)
- The GPU panel requires passwordless SSH to compute nodes (typical on HPC clusters)

## License

MIT
