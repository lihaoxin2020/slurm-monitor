#!/usr/bin/env python3
"""
SLURM Job Monitor — live dashboard for sbatch jobs.
Refreshes every 2 seconds, shows running/pending jobs and tails their latest log output.
Supports side-by-side multi-panel log view for pinned jobs.
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
    stdout_path = details.get("StdOut", "")
    if stdout_path and os.path.isfile(stdout_path):
        return stdout_path
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
    "R": ("RUNNING", 2),
    "PD": ("PENDING", 3),
    "CG": ("COMPLETING", 4),
    "CD": ("COMPLETED", 4),
    "F": ("FAILED", 1),
    "TO": ("TIMEOUT", 1),
    "CA": ("CANCELLED", 5),
    "NF": ("NODE_FAIL", 1),
    "SE": ("SPECIAL_EXIT", 1),
}


def make_bar(pct, width=20):
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def draw_log_panel(stdscr, job_id, col_start, col_width, row_start, panel_height,
                   scroll, log_buffer, is_active):
    """Draw a single log panel for a pinned job. Returns nothing; modifies screen directly."""
    details = get_job_details(job_id)
    log_path = find_log_file(details, job_id)
    job_name = details.get("JobName", job_id)

    row = row_start

    # Panel header
    active_marker = "▶ " if is_active else "  "
    header = f"{active_marker}{job_id}: {job_name}"
    attr = curses.color_pair(7) | curses.A_BOLD if is_active else curses.color_pair(6) | curses.A_BOLD
    stdscr.addnstr(row, col_start, header[:col_width], col_width, attr)
    row += 1

    # Log path
    if row < row_start + panel_height:
        path_display = os.path.basename(log_path) if log_path else "(no log)"
        stdscr.addnstr(row, col_start, path_display[:col_width], col_width, curses.color_pair(6))
        row += 1

    # Log content area
    content_height = panel_height - 2  # subtract header rows
    if content_height <= 0:
        return

    log_lines = tail_file(log_path, n=log_buffer)
    total_log = len(log_lines)

    # Clamp scroll
    max_scroll = max(0, total_log - content_height)
    if scroll > max_scroll:
        scroll = max_scroll
    if scroll < 0:
        scroll = 0

    visible = log_lines[scroll:scroll + content_height]

    # Scroll position indicator
    if total_log > content_height and row < row_start + panel_height:
        pos_pct = int(((scroll + content_height) / total_log) * 100) if total_log > 0 else 100
        pos_info = f"[{scroll+1}-{min(scroll+content_height, total_log)}/{total_log} {pos_pct}%]"
        stdscr.addnstr(row, col_start, pos_info[:col_width], col_width, curses.color_pair(3))
        row += 1
        content_height -= 1

    for ll in visible[:content_height]:
        if row >= row_start + panel_height:
            break
        stdscr.addnstr(row, col_start, ll[:col_width], col_width)
        row += 1

    return scroll  # return clamped scroll value


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
    show_split = False
    scroll_offset = 0
    detail_scroll = 0
    LOG_BUFFER_LINES = 500

    # Split panel state
    pinned_jobs = []          # list of pinned job IDs
    panel_scrolls = {}        # job_id -> scroll offset
    active_panel = 0          # index into pinned_jobs for which panel receives scroll input

    stdscr.nodelay(True)
    stdscr.timeout(REFRESH_INTERVAL * 1000)

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        jobs = get_jobs()
        job_ids = {j["id"] for j in jobs}

        # Remove pinned jobs that no longer exist
        pinned_jobs = [jid for jid in pinned_jobs if jid in job_ids]
        if active_panel >= len(pinned_jobs):
            active_panel = max(0, len(pinned_jobs) - 1)

        # Header
        header = f" SLURM Job Monitor — {USER} "
        ts = time.strftime("%H:%M:%S")
        pin_count = f" pins:{len(pinned_jobs)}" if pinned_jobs else ""
        if show_split and pinned_jobs:
            mode = "SPLIT"
        elif show_detail:
            mode = "DETAIL"
        elif show_gpu:
            mode = "GPU"
        else:
            mode = "LIST"
        header_line = f"{header}| {len(jobs)} job(s) | {ts} | [{mode}]{pin_count} | q:quit d:detail s:split p:pin g:gpu"
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
        pin_col = "PIN"
        col_fmt = f"{pin_col:>3} {'SEL':>3}  {'JOB ID':>10}  {'STATE':<12} {'NAME':<35} {'TIME':>10}  {'TIME LEFT':>10}  {'NODES':>5}  {'NODELIST/REASON':<25}"
        stdscr.addnstr(row, 1, col_fmt[:width-2], width-2, curses.A_BOLD | curses.A_UNDERLINE)
        row += 1

        # Job list
        list_height = height - row - 1
        if show_detail or (show_split and pinned_jobs):
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
            pin_marker = " * " if job["id"] in pinned_jobs else "   "

            line = f"{pin_marker:>3} {marker:>3}  {job['id']:>10}  {label:<12} {job['name']:<35} {job['time']:>10}  {job['timeleft']:>10}  {job['nodes']:>5}  {job['nodelist']:<25}"
            attr = curses.color_pair(color)
            if i == selected:
                attr |= curses.A_BOLD
            stdscr.addnstr(row, 1, line[:width-2], width-2, attr)
            row += 1

        # ── Split panel mode: side-by-side logs ──
        if show_split and pinned_jobs and row < height - 2:
            row += 1
            if row < height - 1:
                separator = "─" * (width - 4)
                stdscr.addnstr(row, 2, separator, width - 4, curses.color_pair(6))
                row += 1

            if row < height - 1:
                hint = "  ←/→: switch panel | j/k PgUp/PgDn: scroll | p: pin/unpin | s: exit split"
                stdscr.addnstr(row, 2, hint[:width-4], width-4, curses.color_pair(3))
                row += 1

            panel_height = height - row
            if panel_height < 3:
                pass  # not enough room
            else:
                n_panels = len(pinned_jobs)
                col_width = (width - 2) // n_panels
                gap = 1  # gap between panels

                for pi, jid in enumerate(pinned_jobs):
                    c_start = 1 + pi * col_width
                    c_w = col_width - gap if pi < n_panels - 1 else col_width
                    is_active = (pi == active_panel)

                    if jid not in panel_scrolls:
                        panel_scrolls[jid] = 0

                    clamped = draw_log_panel(
                        stdscr, jid, c_start, c_w, row, panel_height,
                        panel_scrolls[jid], LOG_BUFFER_LINES, is_active
                    )
                    if clamped is not None:
                        panel_scrolls[jid] = clamped

                    # Draw vertical separator between panels
                    if pi < n_panels - 1:
                        sep_col = c_start + c_w
                        for sr in range(row, min(row + panel_height, height - 1)):
                            try:
                                stdscr.addch(sr, sep_col, curses.ACS_VLINE, curses.color_pair(6))
                            except curses.error:
                                pass

        # ── Single detail panel ──
        elif show_detail and row < height - 2:
            job = jobs[selected]
            details = get_job_details(job["id"])
            log_path = find_log_file(details, job["id"])

            row += 1
            if row < height - 1:
                separator = "─" * (width - 4)
                stdscr.addnstr(row, 2, separator, width - 4, curses.color_pair(6))
                row += 1

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

            if row < height - 2:
                remaining = height - row - 1
                log_lines = tail_file(log_path, n=LOG_BUFFER_LINES)
                total_log = len(log_lines)

                max_scroll = max(0, total_log - remaining)
                if detail_scroll > max_scroll:
                    detail_scroll = max_scroll
                if detail_scroll < 0:
                    detail_scroll = 0

                visible = log_lines[detail_scroll:detail_scroll + remaining]

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

        # ── GPU panel ──
        elif show_gpu and row < height - 2:
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

        # ── Input handling ──
        key = stdscr.getch()
        if key == ord("q"):
            break
        elif key == curses.KEY_UP:
            prev = selected
            selected = max(0, selected - 1)
            if selected != prev:
                detail_scroll = 0
        elif key == curses.KEY_DOWN:
            prev = selected
            selected = min(len(jobs) - 1, selected + 1)
            if selected != prev:
                detail_scroll = 0
        elif key == ord("d"):
            show_detail = not show_detail
            show_gpu = False
            show_split = False
            detail_scroll = 0
        elif key == ord("g"):
            show_gpu = not show_gpu
            show_detail = False
            show_split = False
        elif key == ord("s"):
            # Toggle split view
            if show_split:
                show_split = False
            else:
                show_split = True
                show_detail = False
                show_gpu = False
                # Auto-pin current job if nothing pinned
                if not pinned_jobs and jobs:
                    pinned_jobs.append(jobs[selected]["id"])
                    panel_scrolls[jobs[selected]["id"]] = 0
        elif key == ord("p"):
            # Pin/unpin the currently selected job
            if jobs:
                jid = jobs[selected]["id"]
                if jid in pinned_jobs:
                    pinned_jobs.remove(jid)
                    panel_scrolls.pop(jid, None)
                else:
                    pinned_jobs.append(jid)
                    panel_scrolls[jid] = 0
                # If we just pinned and only have 1, auto-enter split mode
                if len(pinned_jobs) >= 2 and not show_split:
                    show_split = True
                    show_detail = False
                    show_gpu = False
        # Panel navigation (split mode)
        elif key == curses.KEY_LEFT:
            if show_split and pinned_jobs:
                active_panel = max(0, active_panel - 1)
        elif key == curses.KEY_RIGHT:
            if show_split and pinned_jobs:
                active_panel = min(len(pinned_jobs) - 1, active_panel + 1)
        # Scrolling — routes to active panel in split mode or detail_scroll in detail mode
        elif key == curses.KEY_PPAGE or key == ord("k"):
            if show_split and pinned_jobs:
                jid = pinned_jobs[active_panel]
                panel_scrolls[jid] = max(0, panel_scrolls.get(jid, 0) - 10)
            else:
                detail_scroll = max(0, detail_scroll - 10)
        elif key == curses.KEY_NPAGE or key == ord("j"):
            if show_split and pinned_jobs:
                jid = pinned_jobs[active_panel]
                panel_scrolls[jid] = panel_scrolls.get(jid, 0) + 10
            else:
                detail_scroll += 10
        elif key == curses.KEY_HOME:
            if show_split and pinned_jobs:
                jid = pinned_jobs[active_panel]
                panel_scrolls[jid] = 0
            else:
                detail_scroll = 0
        elif key == curses.KEY_END:
            if show_split and pinned_jobs:
                jid = pinned_jobs[active_panel]
                panel_scrolls[jid] = 999999
            else:
                detail_scroll = 999999


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
