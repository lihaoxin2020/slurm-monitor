#!/usr/bin/env python3
"""
SLURM Job Monitor — live dashboard for sbatch jobs.
Refreshes every 2 seconds, shows running/pending jobs and tails their latest log output.
"""

import curses
import subprocess
import time
import os
import glob
import sys
import argparse

USER = os.environ.get("USER", "hl2222")
REFRESH_INTERVAL = 2  # seconds


def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ""


def get_jobs():
    """Get all jobs for current user via squeue."""
    raw = run(
        f'squeue -u {USER} --noheader '
        f'--format="%.18i|%.9P|%.80j|%.2t|%.10M|%.6D|%.30R|%.10L|%.10Q"'
    )
    jobs = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 7:
            jobs.append({
                "id": parts[0],
                "partition": parts[1],
                "name": parts[2],
                "state": parts[3],
                "time": parts[4],
                "nodes": parts[5],
                "nodelist": parts[6],
                "timeleft": parts[7] if len(parts) > 7 else "",
                "priority": parts[8] if len(parts) > 8 else "",
            })
    return jobs


def get_job_details(job_id):
    """Get detailed info for a specific job via scontrol."""
    raw = run(f"scontrol show job {job_id} 2>/dev/null")
    details = {}
    for token in raw.replace("\n", " ").split():
        if "=" in token:
            k, _, v = token.partition("=")
            details[k] = v
    return details


def find_log_file(details, job_id):
    """Try to find the stdout log file for a job."""
    # Check scontrol output first
    stdout_path = details.get("StdOut", "")
    if stdout_path and os.path.isfile(stdout_path):
        return stdout_path

    # Common patterns: slurm-<jobid>.out in home or current working dir
    work_dir = details.get("WorkDir", os.path.expanduser("~"))
    candidates = [
        os.path.join(work_dir, f"slurm-{job_id}.out"),
        os.path.join(work_dir, f"slurm-{job_id}.log"),
        os.path.expanduser(f"~/slurm-{job_id}.out"),
        os.path.expanduser(f"~/slurm-{job_id}.log"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def tail_file(path, n=5):
    """Return last n lines of a file."""
    if not path or not os.path.isfile(path):
        return ["(no log file found)"]
    try:
        r = subprocess.run(
            ["tail", "-n", str(n), path],
            capture_output=True, text=True, timeout=5
        )
        lines = r.stdout.splitlines()
        return lines if lines else ["(log file empty)"]
    except Exception:
        return ["(could not read log)"]


def get_gpu_usage(nodelist):
    """Try to get GPU utilization on allocated nodes."""
    if not nodelist or nodelist.startswith("("):
        return None
    try:
        raw = run(
            f"ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no {nodelist.split(',')[0]} "
            f"'nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total "
            f"--format=csv,noheader,nounits' 2>/dev/null"
        )
        if raw:
            gpus = []
            for line in raw.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    gpus.append({
                        "idx": parts[0],
                        "util": parts[1],
                        "mem_used": parts[2],
                        "mem_total": parts[3],
                    })
            return gpus
    except Exception:
        pass
    return None


STATE_LABELS = {
    "R": ("RUNNING", 2),   # green
    "PD": ("PENDING", 3),  # yellow
    "CG": ("COMPLETING", 4),  # blue
    "CD": ("COMPLETED", 4),
    "F": ("FAILED", 1),    # red
    "TO": ("TIMEOUT", 1),
    "CA": ("CANCELLED", 5),  # magenta
    "NF": ("NODE_FAIL", 1),
    "SE": ("SPECIAL_EXIT", 1),
}


def make_bar(pct, width=20):
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def draw(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_BLUE, -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    curses.init_pair(6, curses.COLOR_CYAN, -1)
    curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)

    selected = 0
    show_detail = False
    show_gpu = False
    scroll_offset = 0
    detail_scroll = 0       # scroll offset within the log tail
    LOG_BUFFER_LINES = 500  # how many lines to read from the log

    stdscr.nodelay(True)
    stdscr.timeout(REFRESH_INTERVAL * 1000)

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        jobs = get_jobs()

        # Header
        header = f" SLURM Job Monitor — {USER} "
        ts = time.strftime("%H:%M:%S")
        header_line = f"{header}| {len(jobs)} job(s) | {ts} | q:quit d:detail g:gpu ↑↓:select"
        stdscr.addnstr(0, 0, header_line.ljust(width), width, curses.color_pair(7) | curses.A_BOLD)

        if not jobs:
            stdscr.addnstr(2, 2, "No jobs found. Waiting...", width - 4, curses.color_pair(3))
            stdscr.refresh()
            key = stdscr.getch()
            if key == ord("q"):
                break
            continue

        # Clamp selection
        if selected >= len(jobs):
            selected = len(jobs) - 1
        if selected < 0:
            selected = 0

        # Table header
        row = 2
        col_fmt = f"{'SEL':>3}  {'JOB ID':>10}  {'STATE':<12} {'NAME':<35} {'TIME':>10}  {'TIME LEFT':>10}  {'NODES':>5}  {'NODELIST/REASON':<25}"
        stdscr.addnstr(row, 1, col_fmt[:width-2], width-2, curses.A_BOLD | curses.A_UNDERLINE)
        row += 1

        # Job list
        list_height = height - row - 1
        if show_detail:
            list_height = min(list_height, max(6, len(jobs) + 1))

        # Adjust scroll
        if selected < scroll_offset:
            scroll_offset = selected
        if selected >= scroll_offset + list_height:
            scroll_offset = selected - list_height + 1

        for i in range(scroll_offset, min(len(jobs), scroll_offset + list_height)):
            if row >= height - 1:
                break
            job = jobs[i]
            st = job["state"]
            label, color = STATE_LABELS.get(st, (st, 0))
            marker = ">>" if i == selected else "  "

            line = f"{marker:>3}  {job['id']:>10}  {label:<12} {job['name']:<35} {job['time']:>10}  {job['timeleft']:>10}  {job['nodes']:>5}  {job['nodelist']:<25}"
            attr = curses.color_pair(color)
            if i == selected:
                attr |= curses.A_BOLD
            stdscr.addnstr(row, 1, line[:width-2], width-2, attr)
            row += 1

        # Detail panel for selected job
        if show_detail and row < height - 2:
            job = jobs[selected]
            details = get_job_details(job["id"])
            log_path = find_log_file(details, job["id"])

            row += 1
            if row < height - 1:
                separator = "─" * (width - 4)
                stdscr.addnstr(row, 2, separator, width - 4, curses.color_pair(6))
                row += 1

            # Job info line
            info_parts = []
            for key in ["JobId", "Partition", "NumCPUs", "Gres", "WorkDir"]:
                if key in details:
                    info_parts.append(f"{key}={details[key]}")
            if row < height - 1:
                stdscr.addnstr(row, 2, "  ".join(info_parts)[:width-4], width-4, curses.color_pair(6))
                row += 1

            if log_path and row < height - 1:
                scroll_hint = "  [PgUp/PgDn or j/k to scroll, Home/End to jump]"
                stdscr.addnstr(row, 2, f"Log: {log_path}{scroll_hint}", width - 4, curses.color_pair(6))
                row += 1

            # Scrollable log view
            if row < height - 2:
                remaining = height - row - 1
                log_lines = tail_file(log_path, n=LOG_BUFFER_LINES)
                total_log = len(log_lines)

                # Clamp detail_scroll
                max_scroll = max(0, total_log - remaining)
                if detail_scroll > max_scroll:
                    detail_scroll = max_scroll
                if detail_scroll < 0:
                    detail_scroll = 0

                # Slice the visible window
                visible = log_lines[detail_scroll:detail_scroll + remaining]

                # Show scroll position indicator
                if row < height - 1 and total_log > remaining:
                    pos_pct = int(((detail_scroll + remaining) / total_log) * 100) if total_log > 0 else 100
                    pos_info = f"── lines {detail_scroll+1}-{min(detail_scroll+remaining, total_log)} of {total_log} ({pos_pct}%) ──"
                    stdscr.addnstr(row, 2, pos_info[:width-4], width-4, curses.color_pair(3))
                    row += 1
                    remaining -= 1

                for ll in visible[:remaining]:
                    if row >= height - 1:
                        break
                    stdscr.addnstr(row, 4, ll[:width-6], width-6)
                    row += 1

        # GPU panel
        if show_gpu and not show_detail and row < height - 2:
            job = jobs[selected]
            row += 1
            if row < height - 1:
                separator = "─" * (width - 4)
                stdscr.addnstr(row, 2, separator, width - 4, curses.color_pair(6))
                row += 1
            if row < height - 1:
                stdscr.addnstr(row, 2, f"GPU usage for {job['nodelist']}...", width-4, curses.color_pair(6))
                row += 1
            gpus = get_gpu_usage(job["nodelist"])
            if gpus:
                for g in gpus:
                    if row >= height - 1:
                        break
                    util = int(g["util"])
                    mem_pct = int(float(g["mem_used"]) / float(g["mem_total"]) * 100) if float(g["mem_total"]) > 0 else 0
                    line = f"  GPU {g['idx']}: {make_bar(util)} {util:3d}%  |  Mem: {make_bar(mem_pct)} {g['mem_used']}/{g['mem_total']} MiB"
                    color = 2 if util > 50 else (3 if util > 10 else 1)
                    stdscr.addnstr(row, 2, line[:width-4], width-4, curses.color_pair(color))
                    row += 1
            elif gpus is None and row < height - 1:
                stdscr.addnstr(row, 2, "(could not fetch GPU info — node may not be accessible via SSH)", width-4, curses.color_pair(3))

        stdscr.refresh()

        # Input handling
        key = stdscr.getch()
        if key == ord("q"):
            break
        elif key == curses.KEY_UP:
            prev = selected
            selected = max(0, selected - 1)
            if selected != prev:
                detail_scroll = 0  # reset log scroll when switching jobs
        elif key == curses.KEY_DOWN:
            prev = selected
            selected = min(len(jobs) - 1, selected + 1)
            if selected != prev:
                detail_scroll = 0
        elif key == ord("d"):
            show_detail = not show_detail
            show_gpu = False
            detail_scroll = 0
        elif key == ord("g"):
            show_gpu = not show_gpu
            show_detail = False
        # Detail panel scrolling
        elif key == curses.KEY_PPAGE or key == ord("k"):  # Page Up / k
            detail_scroll = max(0, detail_scroll - 10)
        elif key == curses.KEY_NPAGE or key == ord("j"):  # Page Down / j
            detail_scroll += 10
        elif key == curses.KEY_HOME:
            detail_scroll = 0
        elif key == curses.KEY_END:
            detail_scroll = 999999  # will be clamped


def main():
    parser = argparse.ArgumentParser(description="SLURM Job Monitor")
    parser.add_argument("-i", "--interval", type=int, default=2, help="Refresh interval in seconds (default: 2)")
    parser.add_argument("-u", "--user", type=str, default=None, help="Monitor jobs for a specific user")
    args = parser.parse_args()

    global REFRESH_INTERVAL, USER
    REFRESH_INTERVAL = args.interval
    if args.user:
        USER = args.user

    curses.wrapper(draw)


if __name__ == "__main__":
    main()
