#!/usr/bin/env python3
"""Core storage, state and actions for the SwiftBar time tracker.

Durability model
----------------
Everything is an append-only JSON-lines event log (``events.jsonl``), flushed
and fsync'd on every write, guarded by an flock. State is rebuilt by replaying
the log, so a hard shutdown can lose at most the event currently mid-write --
never the history.

A running session is closed at the last heartbeat if the heartbeat goes stale
(laptop slept, crashed, SwiftBar quit), so sleep time is not billed to a task.
"""
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
    # A running session whose heartbeat is older than this is assumed to have
    # died with the machine; it gets closed at the last known-alive moment.
    "stale_seconds": 300,
    # False = keep counting across sleep/crash instead of closing the session.
    "close_on_gap": True,
    # Pay period: how long, and a date one of them started on.
    "period_days": 14,
    "period_start": None,
    # A full working day, for the timesheet's "out of" column.
    "hours_per_day": 8,
}


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def ensure_home():
    HOME.mkdir(parents=True, exist_ok=True)
    EXPORTS.mkdir(exist_ok=True)
    if not EVENTS.exists():
        EVENTS.touch()


_LOCK_DEPTH = 0


@contextmanager
def locked():
    """Serialise read-modify-write across processes. Re-entrant in-process:
    flock is per file-description, so a nested open() would deadlock us."""
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
    ensure_home()
    with locked(), open(EVENTS, "a", encoding="utf-8") as fh:
        # A power cut can leave a half-written line with no newline; start a
        # fresh one so the torn record never swallows this event too.
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
                # A torn final line from a power cut: drop it, keep the rest.
                continue
    return out


def config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text()))
        except json.JSONDecodeError:
            pass
    return cfg


def set_config(key, value):
    cfg = config()
    cfg[key] = value
    ensure_home()
    atomic_write(CONFIG, json.dumps(cfg, indent=2))
    return cfg


def atomic_write(path, text):
    """Write via a pid-unique temp so concurrent writers never collide."""
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(text)
    tmp.replace(path)


def beat():
    """Record that the tracker was alive just now. Never fatal."""
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


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.tasks = {}        # id -> dict(name, created, completed_at, deleted)
        self.sessions = []     # dict(task_id, start, end, recovered)
        self.running = None    # dict(task_id, start) or None
        self.recovered = None  # info about a session auto-closed this load
        self.paused = None     # dict(task_id, at): still the current task,
                               # clock not running. Cleared by start/stop/
                               # complete/delete, so only one task is ever
                               # "current" -- running or paused.
        # One "now" per snapshot: a running session must end at the same
        # instant in every reading, or two exports of one state disagree.
        self.now = time.time()

    # -- derived ----------------------------------------------------------
    def active_tasks(self):
        out = [t for t in self.tasks.values()
               if not t["deleted"] and not t["completed_at"]]
        out.sort(key=lambda t: (-self.last_used(t["id"]), t["name"].lower()))
        return out

    def completed_tasks(self):
        out = [t for t in self.tasks.values()
               if not t["deleted"] and t["completed_at"]]
        out.sort(key=lambda t: -t["completed_at"])
        return out

    def last_used(self, task_id):
        stamps = [s["end"] for s in self.sessions if s["task_id"] == task_id]
        if self.running and self.running["task_id"] == task_id:
            stamps.append(self.now)
        return max(stamps) if stamps else 0

    def total(self, task_id, since=None, until=None):
        secs = sum(overlap(s["start"], s["end"], since, until)
                   for s in self.sessions if s["task_id"] == task_id)
        if self.running and self.running["task_id"] == task_id:
            secs += overlap(self.running["start"], self.now, since, until)
        return secs

    def grand_total(self, since=None, until=None):
        secs = sum(overlap(s["start"], s["end"], since, until) for s in self.sessions)
        if self.running:
            secs += overlap(self.running["start"], self.now, since, until)
        return secs

    def elapsed(self):
        return self.now - self.running["start"] if self.running else 0

    def current(self):
        """The task the menu is about: running, else paused, else none."""
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
        "created": ev["ts"],
        "completed_at": None,
        "deleted": False,
        "note": ev.get("note", ""),
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

    for ev in events:
        kind = ev.get("type")
        tid = ev.get("id")
        ts = ev.get("ts", 0)
        if kind == "create":
            st.tasks[tid] = blank_task(ev)
        elif tid is not None and tid not in st.tasks and kind != "create":
            continue  # event for a task we never saw; ignore
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
            was_running = st.running is not None
            close(ev.get("at", ts))
            st.paused = None
            if ev.get("recovered") and was_running:
                st.sessions[-1]["recovered"] = True
                st.recovered = {"task_id": tid, "at": ev.get("at", ts)}
        elif kind == "complete":
            if st.running and st.running["task_id"] == tid:
                close(ts)
            if st.paused and st.paused["task_id"] == tid:
                st.paused = None
            st.tasks[tid]["completed_at"] = ts
        elif kind == "reset":
            # Forget this task's time since the cut. The events stay in the
            # log; replay simply stops counting them, the same way delete
            # keeps history but drops out of the menu.
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
        elif kind == "delete":
            if st.running and st.running["task_id"] == tid:
                close(ts)
            if st.paused and st.paused["task_id"] == tid:
                st.paused = None
            st.tasks[tid]["deleted"] = True
        elif kind == "adjust":
            # manual session insert: {"type":"adjust","id":tid,"start":..,"end":..}
            st.sessions.append({"task_id": tid, "start": ev["start"],
                                "end": ev["end"], "recovered": False})
    return st


def load(recover=True):
    """Replay the log, closing a stale running session if needed."""
    st = replay(read_events())
    if recover and st.running and config()["close_on_gap"]:
        alive = max(last_beat(), st.running["start"])
        gap = time.time() - alive
        if gap > config()["stale_seconds"]:
            append_event(type="stop", id=st.running["task_id"],
                         at=alive, recovered=True,
                         reason="heartbeat stale for %ds" % int(gap))
            st = replay(read_events())
    return st


# --------------------------------------------------------------------------
# actions -- each takes the lock, appends, returns fresh state
# --------------------------------------------------------------------------

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
        if task["completed_at"]:
            append_event(type="reopen", id=task_id)
        if st.running and st.running["task_id"] == task_id:
            return load()
        append_event(type="start", id=task_id)
    return load()


def pause():
    """Close the session but keep the task current, ready to resume."""
    with locked():
        st = load()
        if st.running:
            append_event(type="pause", id=st.running["task_id"])
    return load()


def stop():
    """Close the session and drop the task as current."""
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
            if not st.running:
                return st
            task_id = st.running["task_id"]
        append_event(type="complete", id=task_id)
    return load()


def reset(task_id, since=0.0):
    """Put the task's clock back to zero -- all of its tracked time, not just
    today's. A running task keeps running, from now. The events stay in the
    log; replay stops counting them."""
    with locked():
        append_event(type="reset", id=task_id, since=since)
    return load()


def reopen(task_id):
    with locked():
        append_event(type="reopen", id=task_id)
    return load()


def rename(task_id, name):
    with locked():
        append_event(type="rename", id=task_id, name=name.strip())
    return load()


def delete(task_id):
    with locked():
        append_event(type="delete", id=task_id)
    return load()


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def minutes(seconds):
    """Whole elapsed minutes. Everything on display floors to this -- the
    tracker deals in hours and minutes, never seconds."""
    return int(max(0, seconds) // 60)


def hm(seconds):
    """H:MM, as every total reads."""
    mins = minutes(seconds)
    return "%d:%02d" % (mins // 60, mins % 60)


def hms(seconds):
    """H:MM:SS -- the menu-bar clock, the one place seconds are shown, so a
    running task is visibly running rather than apparently frozen."""
    secs = int(max(0, seconds))
    return "%d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)


def billed(seconds):
    """Hours rounded UP to the next half hour -- what goes on an invoice.
    Anything above zero bills at least 0.5."""
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


def iso(ts):
    return local(ts).strftime("%Y-%m-%d %H:%M")


def day_bounds(offset_days=0):
    d = (datetime.now() + timedelta(days=offset_days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def week_bounds():
    now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = now - timedelta(days=now.weekday())
    return monday.timestamp(), (monday + timedelta(days=7)).timestamp()


def day_ts(d):
    """Midnight at the start of `d`, local time."""
    return datetime(d.year, d.month, d.day).timestamp()


def parse_date(text):
    """YYYY-MM-DD, forgiving about / and . as separators."""
    if not text:
        return None
    text = str(text).strip().replace("/", "-").replace(".", "-")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def default_anchor():
    """Monday of this week, used until a real pay-period start is set."""
    today = date.today()
    return today - timedelta(days=today.weekday())


def period_bounds(offset=0):
    """(since_ts, until_ts, first_day, last_day) for a pay period.
    offset 0 = the one we're in, -1 = the one before it."""
    cfg = config()
    days = max(1, int(cfg.get("period_days") or 14))
    anchor = parse_date(cfg.get("period_start")) or default_anchor()
    index = (date.today() - anchor).days // days  # floors, negatives included
    first = anchor + timedelta(days=(index + offset) * days)
    last = first + timedelta(days=days - 1)
    return day_ts(first), day_ts(last + timedelta(days=1)), first, last


def range_bounds(first, last):
    """Whole days, both ends inclusive."""
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


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def sessions_in(st, since=None, until=None):
    """Sessions overlapping [since, until), each clipped to it. A span across
    a period boundary is split by the clip, so it bills to the period the work
    actually happened in."""
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
    """Split each session at midnight and total the seconds per (day, task).
    Work that runs past midnight lands on the day it was actually done."""
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


def total_today(st, task_id):
    return st.total(task_id, *day_bounds())


def task_of(st, task_id):
    return st.tasks.get(task_id, {"name": "(deleted)",
                                  "completed_at": None, "deleted": True})


def export_path(kind, first=None, last=None, path=None):
    if path:
        return Path(path)
    span = slug_range(first, last) if first else ("all-" + stamp())
    return EXPORTS / ("%s-%s.csv" % (kind, span))


TIMESHEET_HEADER = ["DATE", "TASK NAME", "HOURS"]


def fmt_hours(hours):
    """4.0 -> "4", 3.5 -> "3.5" -- no trailing zeroes on whole hours."""
    return "%g" % round(hours, 2)


def export_timesheet(path=None, state=None, since=None, until=None,
                     first=None, last=None):
    """A day at a time: one row per task, then a total row for the day.
    Hours round up to the next half hour per task per day, which is the
    line item that goes on the sheet."""
    st = state or load()
    path = export_path("timesheet", first, last, path)
    path.parent.mkdir(parents=True, exist_ok=True)

    totals = by_day(sessions_in(st, since, until))
    days = {}
    for (day, task_id), secs in totals.items():
        days.setdefault(day, []).append((task_of(st, task_id)["name"],
                                         billed(secs)))

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(TIMESHEET_HEADER)
        for day in sorted(days):
            rows = sorted(days[day], key=lambda r: (-r[1], r[0].lower()))
            for name, hours in rows:
                w.writerow([day.strftime("%m/%d"), name, fmt_hours(hours)])
            w.writerow(["", "", fmt_hours(sum(h for _, h in rows))])
    return path


def task_status(t):
    if t.get("deleted"):
        return "deleted"
    return "complete" if t.get("completed_at") else "active"


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------
# macOS glue
# --------------------------------------------------------------------------

def osascript(script):
    p = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None  # user cancelled
    return p.stdout.strip()


def ask(prompt, default="", title="Time Tracker"):
    out = osascript(
        'display dialog %s with title %s default answer %s '
        'buttons {"Cancel", "OK"} default button "OK"'
        % (q(prompt), q(title), q(default)))
    if out is None:
        return None
    marker = "text returned:"
    return out.split(marker, 1)[1].strip() if marker in out else ""


def confirm(prompt, title="Time Tracker"):
    return osascript('display dialog %s with title %s buttons {"Cancel", "OK"} '
                     'default button "Cancel" with icon caution'
                     % (q(prompt), q(title))) is not None


NOTICE_SECONDS = 20


def flash(text):
    """Say something without a popup: the menu shows it for NOTICE_SECONDS
    and then forgets it. Used for the things that happen out of sight --
    a bad entry in a dialog, a failed click."""
    try:
        ensure_home()
        atomic_write(NOTICE, "%f\t%s" % (time.time(), str(text).replace("\n", " ")))
    except OSError:
        pass


def notice():
    """The unexpired flash, if there is one."""
    try:
        ts, text = NOTICE.read_text().split("\t", 1)
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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

USAGE = """time tracker

  tt status                      what's running
  tt list [--all]                tasks
  tt new "name"                  create and start
  tt add "name"                  create without starting
  tt start <id|name>             start / switch
  tt pause                       stop the clock, keep the task current
  tt resume                      start the paused task again
  tt stop                        stop the clock
  tt done [id|name]              complete (defaults to what's running)
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
    hits = [t for t in st.tasks.values()
            if not t["deleted"] and needle.lower() in t["name"].lower()]
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        raise SystemExit("no task matching %r" % needle)
    raise SystemExit("ambiguous: " + ", ".join("%s (%s)" % (h["name"], h["id"])
                                               for h in hits))


def cli_export(args, st):
    """export [--period [n] | --from D --to D | --days N | --all] [path]"""
    path = None
    since = until = first = last = None
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag in ("--period", "-p"):
            offset = 0
            if rest and (rest[0].lstrip("-").isdigit()):
                offset = int(rest.pop(0))
            since, until, first, last = period_bounds(-abs(offset))
        elif flag == "--last-period":
            since, until, first, last = period_bounds(-1)
        elif flag in ("--days", "-d"):
            n = int(rest.pop(0))
            last = date.today()
            since, until, first, last = range_bounds(
                last - timedelta(days=n - 1), last)
        elif flag == "--from":
            first = parse_date(rest.pop(0)) or bail(flag)
        elif flag == "--to":
            last = parse_date(rest.pop(0)) or bail(flag)
        elif flag == "--all":
            since = until = first = last = None
        else:
            path = flag
    if first and last and since is None:
        since, until, first, last = range_bounds(first, last)
    elif (first or last) and since is None:
        raise SystemExit("--from needs --to (and vice versa)")
    return export_timesheet(path, st, since, until, first, last)


def bail(flag):
    raise SystemExit("%s wants a date as YYYY-MM-DD" % flag)


def cli(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, args = argv[0], argv[1:]
    st = load()

    if cmd == "status":
        if st.running:
            t = st.tasks[st.running["task_id"]]
            print("%s  %s" % (hms(st.elapsed()), t["name"]))
        elif st.paused:
            t = st.tasks[st.paused["task_id"]]
            print("paused  %s (%s today)"
                  % (t["name"], short(total_today(st, t["id"]))))
        else:
            print("stopped")
    elif cmd == "list":
        show_all = "--all" in args
        for t in st.active_tasks():
            mark = "*" if st.running and st.running["task_id"] == t["id"] else " "
            print("%s %s  %-30s %8s" % (mark, t["id"], t["name"],
                                        short(st.total(t["id"]))))
        if show_all:
            for t in st.completed_tasks():
                print("x %s  %-30s %8s" % (t["id"], t["name"],
                                           short(st.total(t["id"]))))
    elif cmd in ("new", "add"):
        if not args:
            raise SystemExit("name required")
        tid = create_task(args[0], start=(cmd == "new"))
        print(tid)
    elif cmd == "start":
        st = start(resolve(st, args[0]))
        print("started %s" % st.tasks[st.running["task_id"]]["name"])
    elif cmd == "pause":
        if not st.running:
            print("nothing running")
        else:
            name = st.tasks[st.running["task_id"]]["name"]
            el = st.elapsed()
            pause()
            print("paused %s after %s" % (name, short(el)))
    elif cmd == "resume":
        if not st.paused:
            print("nothing paused")
        else:
            st = start(st.paused["task_id"])
            print("resumed %s" % st.tasks[st.running["task_id"]]["name"])
    elif cmd == "stop":
        if st.paused and not st.running:
            name = st.tasks[st.paused["task_id"]]["name"]
            stop()
            print("stopped %s" % name)
        elif not st.running:
            print("nothing running")
        else:
            name = st.tasks[st.running["task_id"]]["name"]
            el = st.elapsed()
            stop()
            print("stopped %s after %s" % (name, short(el)))
    elif cmd == "done":
        tid = resolve(st, args[0]) if args else None
        st2 = complete(tid)
        target = tid or (st.running or {}).get("task_id")
        if target:
            print("completed %s (%s total)" % (st2.tasks[target]["name"],
                                               short(st2.total(target))))
    elif cmd == "reopen":
        reopen(resolve(st, args[0]))
    elif cmd == "rename":
        rename(resolve(st, args[0]), args[1])
    elif cmd == "reset":
        cur = st.running or st.paused or {}
        tid = resolve(st, args[0]) if args else cur.get("task_id")
        if not tid:
            raise SystemExit("nothing running; name a task to reset")
        was = st.total(tid)
        reset(tid)
        print("reset %s — %s forgotten" % (st.tasks[tid]["name"], short(was)))
    elif cmd == "rm":
        delete(resolve(st, args[0]))
    elif cmd == "export":
        print(cli_export(args, st))
    elif cmd == "period":
        if args:
            set_config("period_days", int(args[0]))
            if len(args) > 1:
                d = parse_date(args[1]) or bail("period start")
                set_config("period_start", d.strftime("%Y-%m-%d"))
        for offset, name in ((0, "current"), (-1, "previous")):
            _, _, f, l = period_bounds(offset)
            print("%-9s %s → %s" % (name, f, l))
    elif cmd == "log":
        n = int(args[0]) if args else 20
        for s in st.closed_sessions()[-n:]:
            t = st.tasks.get(s["task_id"], {"name": "(deleted)"})
            print("%s  %s -> %s  %8s  %s" % (
                local(s["start"]).strftime("%a %d %b"),
                local(s["start"]).strftime("%H:%M"),
                local(s["end"]).strftime("%H:%M"),
                short(s["end"] - s["start"]), t["name"]))
    else:
        raise SystemExit(USAGE)
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
