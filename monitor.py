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


def fmt_mem(mb):
    """Format memory in MB to human readable."""
    if mb >= 1048576:
        return f"{mb / 1048576:.1f}T"
    if mb >= 1024:
        return f"{mb / 1024:.0f}G"
    return f"{mb}M"


def fmt_gres(gres_str):
    """Format GRES string compactly, e.g. 'gpu:h200:8' -> '8x h200'."""
    if not gres_str:
        return "—"
    parts = gres_str.split(":")
    if len(parts) >= 3:
        return f"{parts[2]}x {parts[1]}"
    return gres_str


def parse_gpu_counts(gres_str, gres_used_str):
    """Parse GRES and GRES_USED to return (gpu_type, used, total).
    E.g. gres='gpu:b200:8(S:0-1)', gres_used='gpu:b200:6(IDX:0-3,5-6)'
    -> ('b200', 6, 8)
    """
    gpu_type, total, used = "", 0, 0
    if not gres_str:
        return gpu_type, used, total
    # Parse total from GRES: gpu:TYPE:COUNT(...)
    parts = gres_str.split("(")[0].split(":")
    if len(parts) >= 3:
        gpu_type = parts[1]
        try:
            total = int(parts[2])
        except ValueError:
            pass
    elif len(parts) == 2:
        try:
            total = int(parts[1])
        except ValueError:
            pass
    # Parse used from GRES_USED: gpu:TYPE:COUNT(IDX:...)
    if gres_used_str:
        uparts = gres_used_str.split("(")[0].split(":")
        if len(uparts) >= 3:
            try:
                used = int(uparts[2])
            except ValueError:
                pass
        elif len(uparts) == 2:
            try:
                used = int(uparts[1])
            except ValueError:
                pass
    return gpu_type, used, total


def parse_node_state(state_raw):
    """Clean sinfo state string, return base state name."""
    return state_raw.rstrip("*-~#$@+").lower()


NODE_STATE_COLORS = {
    "idle": 2, "mixed": 3, "allocated": 4, "completing": 4,
    "down": 1, "draining": 5, "drained": 5, "error": 1, "fail": 1,
}


def get_partition_summary():
    """Get partition-level resource summary from sinfo, including GPU totals."""
    raw = run('sinfo --noheader --format="%P|%a|%l|%D|%T|%C|%m|%G"')
    partitions = {}
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            continue
        name = parts[0]
        state = parse_node_state(parts[4])
        try:
            nc = int(parts[3])
        except ValueError:
            nc = 0
        if name not in partitions:
            partitions[name] = {
                "name": name, "avail": parts[1], "timelimit": parts[2],
                "idle": 0, "mixed": 0, "alloc": 0, "down": 0, "other": 0,
                "cpus_a": 0, "cpus_i": 0, "cpus_o": 0, "cpus_t": 0,
                "gres": set(),
                "gpu_free": 0, "gpu_total": 0,
            }
        p = partitions[name]
        if "idle" in state:
            p["idle"] += nc
        elif "mix" in state:
            p["mixed"] += nc
        elif "alloc" in state or "complet" in state:
            p["alloc"] += nc
        elif "drain" in state or "down" in state or "fail" in state or "error" in state:
            p["down"] += nc
        else:
            p["other"] += nc
        cpu_parts = parts[5].split("/")
        if len(cpu_parts) == 4:
            try:
                p["cpus_a"] += int(cpu_parts[0])
                p["cpus_i"] += int(cpu_parts[1])
                p["cpus_o"] += int(cpu_parts[2])
                p["cpus_t"] += int(cpu_parts[3])
            except ValueError:
                pass
        gres = parts[7]
        if gres and gres != "(null)":
            p["gres"].add(gres.split("(")[0])
    # Aggregate GPU free/total from per-node data for each partition
    UNAVAILABLE_STATES = {"reserved", "drained", "draining", "down", "error", "fail", "maint", "unknown"}
    for pname, p in partitions.items():
        nodes = get_cluster_nodes(pname)
        for n in nodes:
            p["gpu_total"] += n["gpu_total"]
            if n["state"] not in UNAVAILABLE_STATES:
                p["gpu_free"] += n["gpu_free"]
    result = []
    for name in sorted(partitions.keys()):
        p = partitions[name]
        p["total"] = p["idle"] + p["mixed"] + p["alloc"] + p["down"] + p["other"]
        p["gres"] = sorted(p["gres"])
        result.append(p)
    return result


def get_cluster_nodes(partition_filter=None):
    """Get per-node resource info from sinfo, including GRES usage."""
    raw = run('sinfo -N --noheader -O NodeList:20,Partition:20,StateLong:15,CPUsState:20,Memory:12,FreeMem:12,Gres:50,GresUsed:50')
    seen = set()
    nodes = []
    for line in raw.splitlines():
        # Fixed-width fields: NodeList(20), Partition(20), StateLong(15), CPUsState(20), Memory(12), FreeMem(12), Gres(50), GresUsed(50)
        # Use positional slicing
        if len(line) < 100:
            continue
        name = line[0:20].strip()
        partition = line[20:40].strip()
        state_raw = line[40:55].strip()
        cpus_raw = line[55:75].strip()
        mem_raw = line[75:87].strip()
        freemem_raw = line[87:99].strip()
        gres_raw = line[99:149].strip()
        gres_used_raw = line[149:].strip()

        if partition_filter and partition != partition_filter:
            continue
        if name in seen:
            continue
        seen.add(name)
        state = parse_node_state(state_raw)
        cp = cpus_raw.split("/")
        gpu_type, gpu_used, gpu_total = parse_gpu_counts(gres_raw, gres_used_raw)
        nodes.append({
            "name": name, "partition": partition, "state": state,
            "cpus_a": int(cp[0]) if len(cp) == 4 else 0,
            "cpus_i": int(cp[1]) if len(cp) == 4 else 0,
            "cpus_t": int(cp[3]) if len(cp) == 4 else 0,
            "mem_total": int(mem_raw.rstrip("+")) if mem_raw.rstrip("+").isdigit() else 0,
            "mem_free": int(freemem_raw) if freemem_raw.isdigit() else 0,
            "gres": gres_raw.split("(")[0] if gres_raw and gres_raw != "(null)" else "",
            "gpu_type": gpu_type,
            "gpu_used": gpu_used,
            "gpu_total": gpu_total,
            "gpu_free": gpu_total - gpu_used,
        })
    return nodes


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


def draw(stdscr, init_partitions=None):
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
    show_avail = bool(init_partitions)
    avail_scroll = 0
    avail_filter_idx = 0
    avail_partition_filter = [p.strip() for p in init_partitions.split(",")] if init_partitions else None
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

        # ── Cluster availability view ──
        if show_avail:
            part_data = get_partition_summary()
            if avail_partition_filter:
                part_data = [p for p in part_data if p["name"] in avail_partition_filter]
            all_parts = [p["name"] for p in part_data]
            if avail_filter_idx > len(all_parts):
                avail_filter_idx = 0
            filt_name = all_parts[avail_filter_idx - 1] if avail_filter_idx > 0 else "ALL"

            ts = time.strftime("%H:%M:%S")
            hdr = (f" SLURM Cluster Availability | {ts} | filter:{filt_name}"
                   f" | a:back q:quit f:filter j/k:scroll")
            stdscr.addnstr(0, 0, hdr.ljust(width), width,
                           curses.color_pair(7) | curses.A_BOLD)

            row = 2
            stdscr.addnstr(row, 1, "PARTITION SUMMARY", width - 2,
                           curses.A_BOLD | curses.A_UNDERLINE)
            row += 1
            ph = (f"{'PARTITION':<16} {'AVAIL':>5} {'TIMELIMIT':>10} "
                  f"{'TOTAL':>5} {'IDLE':>5} {'MIX':>5} {'ALLOC':>5} {'DOWN':>5}"
                  f"  {'CPU%':>5} {'':11} {'GPU(Free/Tot)':>14}  {'GPUs'}")
            stdscr.addnstr(row, 1, ph[:width-2], width-2, curses.A_BOLD)
            row += 1

            max_part_rows = min(len(part_data), max(5, height // 3 - 4))
            for p in part_data[:max_part_rows]:
                if row >= height - 1:
                    break
                cpu_pct = (int(p["cpus_a"] / p["cpus_t"] * 100)
                           if p["cpus_t"] > 0 else 0)
                bar = make_bar(cpu_pct, 10)
                gres_list = [fmt_gres(g) for g in p["gres"]]
                gres_str = ", ".join(gres_list) if gres_list else "—"
                gpu_s = f"{p['gpu_free']}/{p['gpu_total']}" if p["gpu_total"] > 0 else "—"
                line = (f"{p['name']:<16} {p['avail']:>5} {p['timelimit']:>10} "
                        f"{p['total']:>5} {p['idle']:>5} {p['mixed']:>5} "
                        f"{p['alloc']:>5} {p['down']:>5}"
                        f"  {cpu_pct:>4}% {bar} {gpu_s:>14}  {gres_str}")
                if p["avail"] != "up":
                    attr = curses.color_pair(1)
                elif p["idle"] > 0:
                    attr = curses.color_pair(2)
                elif p["mixed"] > 0:
                    attr = curses.color_pair(3)
                else:
                    attr = curses.color_pair(4)
                if avail_filter_idx > 0 and p["name"] == filt_name:
                    attr |= curses.A_BOLD
                stdscr.addnstr(row, 1, line[:width-2], width-2, attr)
                row += 1

            if len(part_data) > max_part_rows and row < height - 1:
                stdscr.addnstr(row, 1,
                               f"  ... {len(part_data) - max_part_rows} more partitions",
                               width - 2, curses.color_pair(3))
                row += 1

            row += 1
            if row < height - 1:
                stdscr.addnstr(row, 1, "─" * (width - 2), width - 2,
                               curses.color_pair(6))
                row += 1
            if row < height - 1:
                stdscr.addnstr(row, 1, "NODE DETAILS", width - 2,
                               curses.A_BOLD | curses.A_UNDERLINE)
                row += 1

            nf = (all_parts[avail_filter_idx - 1]
                  if avail_filter_idx > 0 else None)
            node_data = get_cluster_nodes(nf)
            node_data.sort(key=lambda n: (
                {"idle": 0, "mixed": 1, "allocated": 2,
                 "completing": 3}.get(n["state"], 4),
                -n.get("gpu_free", 0),
                n["name"]))

            if row < height - 1:
                nh = (f"{'NODE':<16} {'PARTITION':<16} {'STATE':<12}"
                      f" {'CPUs(A/I/T)':>14} {'MEM(Free/Tot)':>16}"
                      f"  {'GPU(Free/Tot)':>14}  {'GPU Type'}")
                stdscr.addnstr(row, 1, nh[:width-2], width-2, curses.A_BOLD)
                row += 1

            node_area = height - row - 1
            if node_area > 0:
                total_nodes = len(node_data)
                max_sc = max(0, total_nodes - node_area)
                if avail_scroll > max_sc:
                    avail_scroll = max_sc
                if avail_scroll < 0:
                    avail_scroll = 0
                if total_nodes > node_area and row < height - 1:
                    pos_pct = (int(((avail_scroll + node_area)
                                    / total_nodes) * 100)
                               if total_nodes else 100)
                    info = (f"── {avail_scroll+1}-"
                            f"{min(avail_scroll+node_area, total_nodes)}"
                            f" of {total_nodes} nodes ({pos_pct}%) ──")
                    stdscr.addnstr(row, 1, info[:width-2], width-2,
                                   curses.color_pair(3))
                    row += 1
                    node_area -= 1

                for ni in range(avail_scroll,
                                min(avail_scroll + node_area, total_nodes)):
                    if row >= height - 1:
                        break
                    n = node_data[ni]
                    cpu_s = f"{n['cpus_a']}/{n['cpus_i']}/{n['cpus_t']}"
                    mem_s = (f"{fmt_mem(n['mem_free'])}/"
                             f"{fmt_mem(n['mem_total'])}")
                    if n["gpu_total"] > 0:
                        gpu_free = n["gpu_free"]
                        gpu_s = f"{gpu_free}/{n['gpu_total']}"
                        gpu_type_s = n["gpu_type"]
                    else:
                        gpu_s = "—"
                        gpu_type_s = "—"
                    line = (f"{n['name']:<16} {n['partition']:<16}"
                            f" {n['state']:<12} {cpu_s:>14} {mem_s:>16}"
                            f"  {gpu_s:>14}  {gpu_type_s}")
                    color = NODE_STATE_COLORS.get(n["state"], 0)
                    # Highlight nodes with free GPUs in green (skip unschedulable states)
                    if n.get("gpu_free", 0) > 0 and n["state"] not in (
                        "drained", "draining", "down", "error", "fail", "reserved", "maint", "unknown"
                    ):
                        color = 2
                    stdscr.addnstr(row, 1, line[:width-2], width-2,
                                   curses.color_pair(color))
                    row += 1

            stdscr.refresh()
            key = stdscr.getch()
            if key == ord("q"):
                break
            elif key == ord("a"):
                show_avail = False
            elif key == curses.KEY_DOWN or key == ord("j"):
                avail_scroll += 1
            elif key == curses.KEY_UP or key == ord("k"):
                avail_scroll = max(0, avail_scroll - 1)
            elif key == curses.KEY_NPAGE:
                avail_scroll += 20
            elif key == curses.KEY_PPAGE:
                avail_scroll = max(0, avail_scroll - 20)
            elif key == curses.KEY_HOME:
                avail_scroll = 0
            elif key == curses.KEY_END:
                avail_scroll = 999999
            elif key == ord("f"):
                avail_filter_idx = ((avail_filter_idx + 1)
                                    % (len(all_parts) + 1))
                avail_scroll = 0
            continue

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
        header_line = f"{header}| {len(jobs)} job(s) | {ts} | [{mode}]{pin_count} | q:quit a:avail d:detail s:split p:pin g:gpu"
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
        elif key == ord("a"):
            show_avail = True
            show_detail = False
            show_gpu = False
            show_split = False


def main():
    parser = argparse.ArgumentParser(description="SLURM Job Monitor")
    parser.add_argument("-i", "--interval", type=int, default=2, help="Refresh interval in seconds (default: 2)")
    parser.add_argument("-u", "--user", type=str, default=None, help="Monitor jobs for a specific user")
    parser.add_argument("-p", "--partitions", type=str, default=None,
                        help="Launch directly into availability view filtered to these partitions (comma-separated, e.g. gpu_b200,gpu_rtx6000)")
    args = parser.parse_args()

    global REFRESH_INTERVAL, USER
    REFRESH_INTERVAL = args.interval
    if args.user:
        USER = args.user

    curses.wrapper(draw, init_partitions=args.partitions)


if __name__ == "__main__":
    main()
