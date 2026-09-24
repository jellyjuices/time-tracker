#!/usr/bin/env python3
from __future__ import annotations

import csv
import fcntl
import json
import math
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path(os.environ.get("TIME_TRACKER_HOME", str(Path.home() / ".time-tracker")))
EVENTS = HOME / "events.jsonl"
HEARTBEAT = HOME / "heartbeat"
LOCKFILE = HOME / ".lock"
CONFIG = HOME / "config.json"
EXPORTS = HOME / "exports"
NOTICE = HOME / "notice"

DEFAULTS = {
    "stale_seconds": 300,
    "close_on_gap": True,
    "period_days": 14,
    "period_start": None,
    "hours_per_day": 8,
}


def ensure_home():
    HOME.mkdir(parents=True, exist_ok=True)
    EXPORTS.mkdir(exist_ok=True)
    if not EVENTS.exists():
        EVENTS.touch()


_LOCK_DEPTH = 0


@contextmanager
def locked():
    global _LOCK_DEPTH
    if _LOCK_DEPTH:
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
        return
    ensure_home()
    fh = open(LOCKFILE, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _LOCK_DEPTH = 1
        yield
    finally:
        _LOCK_DEPTH = 0
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def ends_with_newline():
    with open(EVENTS, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def append_event(**event):
    event.setdefault("ts", time.time())
    with locked(), open(EVENTS, "a", encoding="utf-8") as fh:
        if EVENTS.stat().st_size and not ends_with_newline():
            fh.write("\n")
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return event


def read_events():
    ensure_home()
    out = []
    with open(EVENTS, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def config():
    cfg = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = None
    if isinstance(saved, dict):
        cfg.update(saved)
    return cfg


def set_config(key, value):
    cfg = config()
    cfg[key] = value
    ensure_home()
    atomic_write(CONFIG, json.dumps(cfg, indent=2))
    return cfg


def atomic_write(path, text):
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def beat():
    try:
        ensure_home()
        atomic_write(HEARTBEAT, str(time.time()))
    except OSError:
        pass


def last_beat():
    try:
        return float(HEARTBEAT.read_text().strip())
    except (OSError, ValueError):
        return 0.0


class State:
    def __init__(self):
        self.tasks = {}
        self.sessions = []
        self.running = None
        self.recovered = None
        self.paused = None
        self.now = time.time()

    def spans(self):
        for s in self.sessions:
            yield s["task_id"], s["start"], s["end"]
        if self.running:
            yield self.running["task_id"], self.running["start"], self.now

    def active_tasks(self):
        last_used = {}
        for tid, _, end in self.spans():
            last_used[tid] = max(last_used.get(tid, 0), end)
        out = [t for t in self.tasks.values()
               if not t["deleted"] and not t["completed_at"]]
        out.sort(key=lambda t: (-last_used.get(t["id"], 0), t["name"].lower()))
        return out

    def completed_tasks(self):
        out = [t for t in self.tasks.values()
               if not t["deleted"] and t["completed_at"]]
        out.sort(key=lambda t: -t["completed_at"])
        return out

    def total(self, task_id, since=None, until=None):
        return sum(overlap(start, end, since, until)
                   for tid, start, end in self.spans() if tid == task_id)

    def grand_total(self, since=None, until=None):
        return sum(overlap(start, end, since, until)
                   for _, start, end in self.spans())

    def elapsed(self):
        return self.now - self.running["start"] if self.running else 0

    def current(self):
        cur = self.running or self.paused
        return self.tasks.get(cur["task_id"]) if cur else None

    def closed_sessions(self, include_running=True):
        rows = list(self.sessions)
        if include_running and self.running:
            rows.append({"task_id": self.running["task_id"],
                         "start": self.running["start"],
                         "end": self.now,
                         "recovered": False,
                         "open": True})
        rows.sort(key=lambda s: s["start"])
        return rows


def overlap(start, end, since, until):
    if since is not None:
        start = max(start, since)
    if until is not None:
        end = min(end, until)
    return max(0.0, end - start)


def blank_task(ev):
    return {
        "id": ev["id"],
        "name": ev.get("name", "Untitled"),
        "created": ev.get("ts", 0),
        "completed_at": None,
        "deleted": False,
        "note": ev.get("note", ""),
        "color": None,
    }


def replay(events):
    st = State()

    def close(at):
        if not st.running:
            return
        start = st.running["start"]
        st.sessions.append({"task_id": st.running["task_id"],
                            "start": start,
                            "end": max(at, start),
                            "recovered": st.running.get("recovered", False)})
        st.running = None

    def drop_current(tid, at):
        if st.running and st.running["task_id"] == tid:
            close(at)
        if st.paused and st.paused["task_id"] == tid:
            st.paused = None

    for ev in events:
        kind = ev.get("type")
        tid = ev.get("id")
        ts = ev.get("ts", 0)
        if kind == "create":
            st.tasks[tid] = blank_task(ev)
            continue
        if tid not in st.tasks:
            continue

        if kind == "start":
            close(ts)
            st.running = {"task_id": tid, "start": ts}
            st.paused = None
        elif kind == "pause":
            at = ev.get("at", ts)
            if st.running and st.running["task_id"] == tid:
                close(at)
                st.paused = {"task_id": tid, "at": at}
        elif kind == "stop":
            at = ev.get("at", ts)
            was_running = st.running is not None
            close(at)
            st.paused = None
            if ev.get("recovered") and was_running:
                st.sessions[-1]["recovered"] = True
                st.recovered = {"task_id": tid, "at": at}
        elif kind == "complete":
            drop_current(tid, ts)
            st.tasks[tid]["completed_at"] = ts
        elif kind == "reset":
            cut = ev.get("since", ts)
            kept = []
            for sess in st.sessions:
                if sess["task_id"] != tid or sess["end"] <= cut:
                    kept.append(sess)
                elif sess["start"] < cut:
                    kept.append(dict(sess, end=cut))
            st.sessions = kept
            if st.running and st.running["task_id"] == tid:
                st.running = dict(st.running, start=max(ts, cut))
        elif kind == "reopen":
            st.tasks[tid]["completed_at"] = None
        elif kind == "rename":
            st.tasks[tid]["name"] = ev["name"]
        elif kind == "color":
            st.tasks[tid]["color"] = ev.get("color")
        elif kind == "delete":
            drop_current(tid, ts)
            st.tasks[tid]["deleted"] = True
        elif kind == "adjust":
            st.sessions.append({"task_id": tid, "start": ev["start"],
                                "end": ev["end"], "recovered": False})
    return st


def stale_since(st):
    if not st.running:
        return None
    cfg = config()
    if not cfg["close_on_gap"]:
        return None
    alive = max(last_beat(), st.running["start"])
    return alive if time.time() - alive > cfg["stale_seconds"] else None


def load(recover=True):
    st = replay(read_events())
    if not recover or stale_since(st) is None:
        return st
    with locked():
        st = replay(read_events())
        alive = stale_since(st)
        if alive is not None:
            append_event(type="stop", id=st.running["task_id"],
                         at=alive, recovered=True,
                         reason="heartbeat stale for %ds" % int(time.time() - alive))
            st = replay(read_events())
    return st


def new_id():
    return uuid.uuid4().hex[:8]


def create_task(name, start=False):
    tid = new_id()
    with locked():
        append_event(type="create", id=tid, name=name.strip() or "Untitled")
        if start:
            append_event(type="start", id=tid)
    return tid


def start(task_id):
    with locked():
        st = load()
        task = st.tasks.get(task_id)
        if not task or task["deleted"]:
            raise KeyError("no such task: %s" % task_id)
        if st.running and st.running["task_id"] == task_id:
            return st
        if task["completed_at"]:
            append_event(type="reopen", id=task_id)
        append_event(type="start", id=task_id)
    return load()


def pause():
    with locked():
        st = load()
        if st.running:
            append_event(type="pause", id=st.running["task_id"])
    return load()


def stop():
    with locked():
        st = load()
        cur = st.running or st.paused
        if cur:
            append_event(type="stop", id=cur["task_id"])
    return load()


def complete(task_id=None):
    with locked():
        st = load()
        if task_id is None:
            cur = st.running or st.paused
            if not cur:
                return st
            task_id = cur["task_id"]
        append_event(type="complete", id=task_id)
    return load()


def reset(task_id, since=0.0):
    append_event(type="reset", id=task_id, since=since)
    return load()


def reopen(task_id):
    append_event(type="reopen", id=task_id)
    return load()


COLORS = {
    "Red": 160,
    "Orange": 208,
    "Yellow": 220,
    "Green": 34,
    "Teal": 36,
    "Blue": 33,
    "Purple": 99,
    "Pink": 162,
}


def set_color(task_id, color):
    append_event(type="color", id=task_id,
                 color=color if color in COLORS else None)
    return load()


def rename(task_id, name):
    name = name.strip()
    if name:
        append_event(type="rename", id=task_id, name=name)
    return load()


def delete(task_id):
    append_event(type="delete", id=task_id)
    return load()


def minutes(seconds):
    return int(max(0, seconds) // 60)


def hm(seconds):
    mins = minutes(seconds)
    return "%d:%02d" % (mins // 60, mins % 60)


def hms(seconds):
    secs = int(max(0, seconds))
    return "%d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)


def billed(seconds):
    return math.ceil(max(0.0, seconds) / 1800.0) * 0.5


def short(seconds):
    h, m = divmod(minutes(seconds), 60)
    if h and m:
        return "%dh %dm" % (h, m)
    if h:
        return "%dh" % h
    return "%dm" % m


def local(ts):
    return datetime.fromtimestamp(ts)


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def day_bounds(offset_days=0):
    d = (datetime.now() + timedelta(days=offset_days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def week_bounds():
    now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = now - timedelta(days=now.weekday())
    return monday.timestamp(), (monday + timedelta(days=7)).timestamp()


def day_ts(d):
    return datetime(d.year, d.month, d.day).timestamp()


def parse_date(text):
    if not text:
        return None
    text = str(text).strip().replace("/", "-").replace(".", "-")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def default_anchor():
    today = date.today()
    return today - timedelta(days=today.weekday())


def period_bounds(offset=0):
    cfg = config()
    days = max(1, int(cfg.get("period_days") or 14))
    anchor = parse_date(cfg.get("period_start")) or default_anchor()
    index = (date.today() - anchor).days // days
    first = anchor + timedelta(days=(index + offset) * days)
    last = first + timedelta(days=days - 1)
    return day_ts(first), day_ts(last + timedelta(days=1)), first, last


def range_bounds(first, last):
    if last < first:
        first, last = last, first
    return day_ts(first), day_ts(last + timedelta(days=1)), first, last


def label_range(first, last):
    if first == last:
        return first.strftime("%-d %b")
    if (first.year, first.month) == (last.year, last.month):
        return "%s–%s" % (first.strftime("%-d"), last.strftime("%-d %b"))
    return "%s – %s" % (first.strftime("%-d %b"), last.strftime("%-d %b"))


def slug_range(first, last):
    return "%s_%s" % (first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d"))


def total_today(st, task_id):
    return st.total(task_id, *day_bounds())


def sessions_in(st, since=None, until=None):
    out = []
    for s in st.closed_sessions():
        start = max(s["start"], since) if since is not None else s["start"]
        end = min(s["end"], until) if until is not None else s["end"]
        if end <= start:
            continue
        out.append(dict(s, start=start, end=end,
                        clipped=(start > s["start"] or end < s["end"])))
    return out


def by_day(sessions):
    totals = {}
    for s in sessions:
        cursor = s["start"]
        while cursor < s["end"]:
            day = local(cursor).date()
            midnight = day_ts(day + timedelta(days=1))
            chunk_end = min(s["end"], midnight)
            key = (day, s["task_id"])
            totals[key] = totals.get(key, 0.0) + (chunk_end - cursor)
            cursor = chunk_end
    return totals


def task_name(st, task_id):
    task = st.tasks.get(task_id)
    return task["name"] if task else "(deleted)"


def export_path(kind, first=None, last=None, path=None):
    if path:
        return Path(path)
    span = slug_range(first, last) if first else ("all-" + stamp())
    return EXPORTS / ("%s-%s.csv" % (kind, span))


TIMESHEET_HEADER = ["DATE", "TASK NAME", "HOURS"]


def fmt_hours(hours):
    return "%g" % round(hours, 2)


def export_timesheet(path=None, state=None, since=None, until=None,
                     first=None, last=None):
    st = state if state is not None else load()
    path = export_path("timesheet", first, last, path)
    path.parent.mkdir(parents=True, exist_ok=True)

    days = {}
    for (day, task_id), secs in by_day(sessions_in(st, since, until)).items():
        days.setdefault(day, []).append((task_name(st, task_id), billed(secs)))

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(TIMESHEET_HEADER)
        for day in sorted(days):
            rows = sorted(days[day], key=lambda r: (-r[1], r[0].lower()))
            for name, hours in rows:
                w.writerow([day.strftime("%m/%d"), name, fmt_hours(hours)])
            w.writerow(["", "", fmt_hours(sum(h for _, h in rows))])
    return path


def osascript(script):
    p = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def ask(prompt, default="", title="Time Tracker"):
    return osascript(
        'text returned of (display dialog %s with title %s default answer %s '
        'buttons {"Cancel", "OK"} default button "OK")'
        % (q(prompt), q(title), q(default)))


def confirm(prompt, title="Time Tracker"):
    return osascript('display dialog %s with title %s buttons {"Cancel", "OK"} '
                     'default button "Cancel" with icon caution'
                     % (q(prompt), q(title))) is not None


NOTICE_SECONDS = 20


def flash(text):
    try:
        ensure_home()
        atomic_write(NOTICE, "%f\t%s" % (time.time(), str(text).replace("\n", " ")))
    except OSError:
        pass


def notice():
    try:
        ts, text = NOTICE.read_text(encoding="utf-8").split("\t", 1)
        return text if time.time() - float(ts) < NOTICE_SECONDS else None
    except (OSError, ValueError):
        return None


def save_dialog(default_name):
    out = osascript('POSIX path of (choose file name with prompt "Export CSV" '
                    'default name %s)' % q(default_name))
    return Path(out) if out else None


def q(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def reveal(path):
    subprocess.run(["open", "-R", str(path)])


USAGE = """time tracker

  tt status                      what's running
  tt list [--all]                tasks
  tt new "name"                  create and start
  tt add "name"                  create without starting
  tt start <id|name>             start / switch
  tt pause                       stop the clock, keep the task current
  tt resume                      start the paused task again
  tt stop                        stop the clock
  tt done [id|name]              complete (defaults to the current task)
  tt reopen <id|name>
  tt rename <id|name> "new"
  tt reset [id|name]             put the task's clock back to zero
  tt rm <id|name>
  tt export [range] [path]       per-day timesheet CSV
       range: --period [n]   this pay period (n back, e.g. --period 1)
              --from D --to D   inclusive days, YYYY-MM-DD
              --days N       the last N days
              --all          everything (the default)
  tt period [days] [start]       show or set the pay period
  tt log [n]                     recent sessions
"""


def resolve(st, needle):
    if needle in st.tasks:
        return needle
    live = [t for t in st.tasks.values() if not t["deleted"]]
    lowered = needle.lower()
    hits = ([t for t in live if t["name"].lower() == lowered]
            or [t for t in live if lowered in t["name"].lower()])
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        raise SystemExit("no task matching %r" % needle)
    raise SystemExit("ambiguous: " + ", ".join("%s (%s)" % (h["name"], h["id"])
                                               for h in hits))


def need(args, n, usage):
    if len(args) < n:
        raise SystemExit("usage: tt " + usage)
    return args


def positive_int(text, what):
    try:
        n = int(text)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        raise SystemExit("%s wants a whole number above zero" % what)
    return n


def bail(flag):
    raise SystemExit("%s wants a date as YYYY-MM-DD" % flag)


def current_id(st):
    cur = st.running or st.paused
    return cur["task_id"] if cur else None


def cli_export(args, st):
    path = None
    since = until = first = last = None
    rest = list(args)

    def take(flag):
        if not rest:
            raise SystemExit("%s needs a value" % flag)
        return rest.pop(0)

    while rest:
        flag = rest.pop(0)
        if flag in ("--period", "-p"):
            offset = 0
            if rest and rest[0].lstrip("-").isdigit():
                offset = int(rest.pop(0))
            since, until, first, last = period_bounds(-abs(offset))
        elif flag == "--last-period":
            since, until, first, last = period_bounds(-1)
        elif flag in ("--days", "-d"):
            n = positive_int(take(flag), flag)
            today = date.today()
            since, until, first, last = range_bounds(
                today - timedelta(days=n - 1), today)
        elif flag == "--from":
            first = parse_date(take(flag)) or bail(flag)
        elif flag == "--to":
            last = parse_date(take(flag)) or bail(flag)
        elif flag == "--all":
            since = until = first = last = None
        else:
            path = flag
    if since is None and first and last:
        since, until, first, last = range_bounds(first, last)
    elif since is None and (first or last):
        raise SystemExit("--from needs --to (and vice versa)")
    return export_timesheet(path, st, since, until, first, last)


def cmd_status(st, args):
    if st.running:
        print("%s  %s" % (hms(st.elapsed()), task_name(st, st.running["task_id"])))
    elif st.paused:
        tid = st.paused["task_id"]
        print("paused  %s (%s today)" % (task_name(st, tid),
                                         short(total_today(st, tid))))
    else:
        print("stopped")


def cmd_list(st, args):
    running = st.running["task_id"] if st.running else None
    for t in st.active_tasks():
        mark = "*" if t["id"] == running else " "
        print("%s %s  %-30s %8s" % (mark, t["id"], t["name"],
                                    short(st.total(t["id"]))))
    if "--all" in args:
        for t in st.completed_tasks():
            print("x %s  %-30s %8s" % (t["id"], t["name"],
                                       short(st.total(t["id"]))))


def cmd_new(st, args, start_now=True):
    name = need(args, 1, '%s "name"' % ("new" if start_now else "add"))[0]
    print(create_task(name, start=start_now))


def cmd_add(st, args):
    cmd_new(st, args, start_now=False)


def cmd_start(st, args):
    st = start(resolve(st, need(args, 1, "start <id|name>")[0]))
    print("started %s" % task_name(st, st.running["task_id"]))


def cmd_pause(st, args):
    if not st.running:
        print("nothing running")
        return
    name = task_name(st, st.running["task_id"])
    elapsed = st.elapsed()
    pause()
    print("paused %s after %s" % (name, short(elapsed)))


def cmd_resume(st, args):
    if not st.paused:
        print("nothing paused")
        return
    st = start(st.paused["task_id"])
    print("resumed %s" % task_name(st, st.running["task_id"]))


def cmd_stop(st, args):
    if st.running:
        name = task_name(st, st.running["task_id"])
        elapsed = st.elapsed()
        stop()
        print("stopped %s after %s" % (name, short(elapsed)))
    elif st.paused:
        name = task_name(st, st.paused["task_id"])
        stop()
        print("stopped %s" % name)
    else:
        print("nothing running")


def cmd_done(st, args):
    tid = resolve(st, args[0]) if args else current_id(st)
    if not tid:
        print("nothing running")
        return
    st = complete(tid)
    print("completed %s (%s total)" % (task_name(st, tid), short(st.total(tid))))


def cmd_reopen(st, args):
    reopen(resolve(st, need(args, 1, "reopen <id|name>")[0]))


def cmd_rename(st, args):
    needle, name = need(args, 2, 'rename <id|name> "new"')[:2]
    rename(resolve(st, needle), name)


def cmd_reset(st, args):
    tid = resolve(st, args[0]) if args else current_id(st)
    if not tid:
        raise SystemExit("nothing running; name a task to reset")
    was = st.total(tid)
    reset(tid)
    print("reset %s — %s forgotten" % (task_name(st, tid), short(was)))


def cmd_rm(st, args):
    delete(resolve(st, need(args, 1, "rm <id|name>")[0]))


def cmd_export(st, args):
    print(cli_export(args, st))


def cmd_period(st, args):
    if args:
        set_config("period_days", positive_int(args[0], "period length"))
        if len(args) > 1:
            d = parse_date(args[1]) or bail("period start")
            set_config("period_start", d.strftime("%Y-%m-%d"))
    for offset, name in ((0, "current"), (-1, "previous")):
        _, _, first, last = period_bounds(offset)
        print("%-9s %s → %s" % (name, first, last))


def cmd_log(st, args):
    n = positive_int(args[0], "log length") if args else 20
    for s in st.closed_sessions()[-n:]:
        print("%s  %s -> %s  %8s  %s" % (
            local(s["start"]).strftime("%a %d %b"),
            local(s["start"]).strftime("%H:%M"),
            local(s["end"]).strftime("%H:%M"),
            short(s["end"] - s["start"]), task_name(st, s["task_id"])))


COMMANDS = {
    "status": cmd_status,
    "list": cmd_list,
    "new": cmd_new,
    "add": cmd_add,
    "start": cmd_start,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "stop": cmd_stop,
    "done": cmd_done,
    "reopen": cmd_reopen,
    "rename": cmd_rename,
    "reset": cmd_reset,
    "rm": cmd_rm,
    "export": cmd_export,
    "period": cmd_period,
    "log": cmd_log,
}


def cli(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    handler = COMMANDS.get(argv[0])
    if handler is None:
        raise SystemExit(USAGE)
    handler(load(), argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
