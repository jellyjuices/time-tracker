#!/usr/bin/env python3
# <bitbar.title>Time Tracker</bitbar.title>
# <bitbar.version>v1.1</bitbar.version>
# <bitbar.author>jj</bitbar.author>
# <bitbar.desc>Time tracking with one running task at a time, crash-safe event log, pay-period CSV export.</bitbar.desc>
# <bitbar.dependencies>python3</bitbar.dependencies>
# <swiftbar.type>streamable</swiftbar.type>
# <swiftbar.useTrailingStreamSeparator>true</swiftbar.useTrailingStreamSeparator>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
"""SwiftBar front end.

No args = stream the menu, one block a second, so the clock in the menu bar
counts up instead of sitting still between refreshes. Args = do a thing.

The 15s in the filename is SwiftBar's refresh interval, which a streamable
plugin ignores -- it only matters if the stream ever has to fall back.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(os.path.realpath(__file__)).parent))
import tt_core as tt  # noqa: E402

SELF = os.path.abspath(__file__)
RUN = "bash=%s terminal=false refresh=true" % SELF
MAX_TITLE = 24

TICK = 1.0        # seconds between menu-bar updates
RELOAD = 15.0     # re-read the log at least this often, log changes aside
BEAT = 30.0       # how often a running session says it is still alive
GREY = "color=#888888"
ORANGE = "#e8590c"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def esc(text):
    return str(text).replace("|", "¦").replace("\n", " ")


_printed = 0
_sep_pending = False


def line(text, *params, **kw):
    """Emitting a line first flushes any separator waiting on it, so a
    section that renders nothing takes its divider with it."""
    global _printed, _sep_pending
    depth = kw.pop("depth", 0)
    if _sep_pending and _printed:
        print("---")
    _sep_pending = False
    prefix = "--" * depth
    parts = [p for p in params if p]
    body = esc(text)
    print(prefix + (body + " | " + " ".join(parts) if parts else body))
    _printed += 1


def sep(depth=0):
    """Ask for a divider before whatever comes next -- if anything does."""
    global _sep_pending
    if depth:
        print("--" * depth + "---")
    else:
        _sep_pending = True


def action(text, *args, **kw):
    """A clickable item that re-invokes this script with `args`."""
    params = [RUN] + ["param%d=%s" % (i + 1, shellish(a))
                      for i, a in enumerate(args)]
    line(text, *(params + list(kw.pop("extra", []))), **kw)


def header(text, *extra, **kw):
    """A section label. macOS dims any item with no action attached, so a
    bare label renders greyed out -- the noop action keeps it at full
    strength. Clicking it just refreshes."""
    action("**" +text + "**", "md=True", "noop", extra=list(extra), **kw)


def shellish(value):
    value = str(value)
    return '"%s"' % value if (" " in value or not value) else value


def trunc(text, n=MAX_TITLE):
    text = str(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + ""


def render_exports():
    """Export ▸ one timesheet per window -- pick the dates, get the file."""
    header("Export timesheet")
    windows = []
    for offset, name in ((0, "This pay period"), (-1, "Last pay period")):
        _, _, first, last = tt.period_bounds(offset)
        windows.append(("%s · %s" % (name, tt.label_range(first, last)),
                        ["period", str(offset)]))
    windows.append(("Custom dates", ["range"]))
    windows.append(("Everything", ["all"]))
    for label, scope in windows:
        action(label, "export", *scope, depth=1)


def render_current(st):
    """The current task and the four things you do to it. Whatever is
    current -- running or paused -- owns this block, so the rest of the menu
    stays out of the way."""
    task = st.current()
    if not task:
        return
    header(task["name"], "size=13")
    if st.running:
        line("since %s · %s this session" % (
            tt.local(st.running["start"]).strftime("%H:%M"),
            tt.short(st.elapsed())), "size=11", GREY)
        sep()
        action("Pause", "pause", extra=["sfimage=pause.circle"])
    else:
        line("paused at %s · %s today" % (
            tt.local(st.paused["at"]).strftime("%H:%M"),
            tt.short(tt.total_today(st, task["id"]))), "size=11", GREY)
        sep()
        action("Resume", "start", task["id"], extra=["sfimage=play.circle"])
    action("Stop", "stop", extra=["sfimage=stop.circle"])
    sep()
    action("Finish", "complete", task["id"],
           extra=["sfimage=checkmark.circle"])
    header("Options", "sfimage=ellipsis.circle")
    action("Rename", "rename", task["id"], depth=1,
           extra=["sfimage=pencil"])
    action("Reset current session", "reset", task["id"], depth=1,
           extra=["sfimage=arrow.counterclockwise"])
    action("Delete", "delete", task["id"], depth=1,
           extra=["sfimage=trash"])


def render(st):
    global _printed, _sep_pending
    _printed, _sep_pending = 0, False

    current = st.current()

    # ---- menu bar title -------------------------------------------------
    # The clock is today's time on the current task, so it keeps counting
    # across a pause instead of restarting with each session.
    if current:
        clock = tt.hms(tt.total_today(st, current["id"]))
        if st.running:
            line("%s  %s" % (trunc(current["name"]), clock),
                 "sfimage=record.circle.fill", "sfcolor=" + ORANGE,
                 "font=Menlo", "size=13")
        else:
            line("%s  %s" % (trunc(current["name"]), clock),
                 "sfimage=pause.circle", "sfcolor=#888888",
                 "font=Menlo", "size=13")
    else:
        line("", "sfimage=stopwatch", "size=13")
    sep()
    # macOS dims any item with no action, which is what greys a bare
    # label. The noop action gives it a target so it renders at full
    # strength; clicking it just refreshes.
    header("Time Tracker", "md=True", "size=14", "color=#1d1d1f,#f5f5f7")
    today = tt.day_bounds()
    week = tt.week_bounds()
    if tt.minutes(st.grand_total(*week)):
        line("Today  %s      Week  %s" % (tt.short(st.grand_total(*today)),
                                          tt.short(st.grand_total(*week))),
             "size=12", GREY)
    sep()

    notice = tt.notice()
    if notice:
        line("· %s" % trunc(notice, 44), "size=11", "color=#c92a2a")
        sep()

    if st.recovered:
        name = st.tasks.get(st.recovered["task_id"], {}).get("name", "?")
        line("⚠︎ recovered: %s closed at %s" % (
            trunc(name, 18), tt.local(st.recovered["at"]).strftime("%a %H:%M")),
            "size=11", "color=#c92a2a")
        sep()

    # ---- current task ---------------------------------------------------
    # A current task owns the whole menu: everything else would just be a way
    # to lose track of what is being counted. Stop, and the rest comes back.
    if current:
        render_current(st)
        return

    # ---- start ----------------------------------------------------------
    others = st.active_tasks()
    if others:
        line("Start", "size=11", GREY)
    for task in others:
        action("○  %-26s %8s" % (trunc(task["name"], 26),
                                 tt.short(st.total(task["id"]))),
               "start", task["id"], extra=["font=Menlo", "size=12"])
    sep()
    action("New task", "new", extra=["sfimage=plus.circle"])

    # ---- manage ---------------------------------------------------------
    sep()
    if others:
        header("Manage")
        for task in others:
            header(trunc(task["name"], 28), depth=1)
            action("Rename", "rename", task["id"], depth=2)
            action("Mark complete", "complete", task["id"], depth=2)
            action("Delete", "delete", task["id"], depth=2)

    done = st.completed_tasks()
    if done:
        header("Completed (%d)" % len(done))
        for task in done[:25]:
            header("%s — %s" % (trunc(task["name"], 26),
                                tt.short(st.total(task["id"]))), depth=1)
            action("Reopen & start", "start", task["id"], depth=2)
            action("Reopen only", "reopen", task["id"], depth=2)
            action("Delete", "delete", task["id"], depth=2)

    # ---- footer ---------------------------------------------------------
    sep()
    if st.closed_sessions():
        render_exports()
    sep()
    # A parent with only styling params renders as a disabled heading;
    # an action gives it a target so macOS keeps it live.
    header("Config")
    _, _, pf, pl = tt.period_bounds(0)
    _, _, nf, _ = tt.period_bounds(1)
    line("Pay period: %d days · Current: %s · Next: %s"
         % (tt.config()["period_days"], tt.label_range(pf, pl),
            nf.strftime("%-d %b")),
         "size=11", GREY, depth=1)
    action("Change pay period", "set-period", depth=1)
    action("Open data folder", "open-folder", depth=1,
           extra=["sfimage=rectangle.portrait.and.arrow.right"])
    action("Refresh", "noop", depth=1, extra=["sfimage=arrow.clockwise"])


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------

def log_sig():
    """Cheap fingerprint of the event log: changes when anyone writes."""
    try:
        s = tt.EVENTS.stat()
        return (s.st_mtime, s.st_size)
    except OSError:
        return None


def stream():
    """One menu block a second, forever. State is only replayed when the log
    actually changed (or every RELOAD seconds anyway, to pick up midnight and
    a stale heartbeat) -- a tick is otherwise just arithmetic on the snapshot
    we already hold."""
    st, sig, loaded, beat = tt.load(), log_sig(), time.time(), 0.0
    while True:
        now = time.time()
        if sig != log_sig() or now - loaded > RELOAD:
            st = tt.load()
            sig, loaded = log_sig(), now
        if st.running and now - beat > BEAT:
            tt.beat()
            beat = now
        st.now = now
        render(st)
        print("~~~", flush=True)
        time.sleep(TICK - (time.time() % TICK))  # keep ticks on the second


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

def do(argv):
    cmd, rest = argv[0], argv[1:]
    st = tt.load()

    if cmd == "start":
        tt.start(rest[0])
        tt.beat()

    elif cmd == "pause":
        tt.pause()

    elif cmd == "stop":
        tt.stop()

    elif cmd == "complete":
        tid = rest[0] if rest else (st.running or st.paused or {}).get("task_id")
        if tid:
            tt.complete(tid)

    elif cmd == "reopen":
        tt.reopen(rest[0])

    elif cmd == "new":
        name = tt.ask("Name for the new task:")
        if not name:
            return
        tt.create_task(name, start=True)
        tt.beat()

    elif cmd == "rename":
        task = st.tasks[rest[0]]
        name = tt.ask("Rename task:", task["name"])
        if name:
            tt.rename(rest[0], name)

    elif cmd == "reset":
        task = st.tasks[rest[0]]
        if tt.confirm("Reset “%s”?\n\nAll %s ever tracked against it stops "
                      "counting and the clock starts again from zero."
                      % (task["name"], tt.short(st.total(rest[0])))):
            tt.reset(rest[0])

    elif cmd == "delete":
        task = st.tasks[rest[0]]
        if tt.confirm("Delete “%s”?\n\nIts %s of tracked time stays in the log "
                      "but drops out of the menu."
                      % (task["name"], tt.short(st.total(rest[0])))):
            tt.delete(rest[0])

    elif cmd == "export":
        scope = rest[0] if rest else "all"
        since = until = first = last = None
        if scope == "period":
            since, until, first, last = tt.period_bounds(int(rest[1]))
        elif scope == "range":
            first = tt.parse_date(tt.ask(
                "Export from which day?  (YYYY-MM-DD)",
                (date.today() - timedelta(days=13)).strftime("%Y-%m-%d")))
            if not first:
                return
            last = tt.parse_date(tt.ask("up to and including which day?",
                                        date.today().strftime("%Y-%m-%d")))
            if not last:
                return
            since, until, first, last = tt.range_bounds(first, last)
        span = tt.slug_range(first, last) if first else "all-" + tt.stamp()
        path = tt.save_dialog("timesheet-%s.csv" % span)
        if not path:
            return
        written = tt.export_timesheet(path, st, since, until, first, last)
        tt.flash("Exported %s" % written.name)
        tt.reveal(written)  # Finder is the receipt

    elif cmd == "set-period":
        cfg = tt.config()
        days = tt.ask("How many days is a pay period?", str(cfg["period_days"]))
        if days is None:
            return
        if not days.strip().isdigit() or int(days.strip()) < 1:
            tt.flash("“%s” is not a number of days — pay period unchanged" % days)
            return
        _, _, current_first, _ = tt.period_bounds(0)
        start = tt.ask("Any one date a pay period starts on:\n\n"
                       "(YYYY-MM-DD — periods are counted forward and back "
                       "from here, so a future date is fine)",
                       (tt.parse_date(cfg.get("period_start"))
                        or current_first).strftime("%Y-%m-%d"))
        if start is None:
            return
        parsed = tt.parse_date(start)
        if not parsed:
            tt.flash("“%s” is not a date as YYYY-MM-DD — pay period unchanged"
                     % start)
            return
        tt.set_config("period_days", int(days.strip()))
        tt.set_config("period_start", parsed.strftime("%Y-%m-%d"))

    elif cmd == "open-folder":
        tt.subprocess.run(["open", str(tt.HOME)])

    elif cmd == "toggle-gap":
        tt.set_config("close_on_gap", not tt.config()["close_on_gap"])

    elif cmd == "noop":
        pass

    else:  # fall through to the CLI for anything else
        tt.cli(argv)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            do(sys.argv[1:])
        except SystemExit:
            raise
        except Exception as exc:  # a click should never leave a stack trace
            tt.flash("%s: %s" % (type(exc).__name__, exc))
            sys.exit(1)
    else:
        try:
            stream()
        except (KeyboardInterrupt, BrokenPipeError):
            os._exit(0)  # SwiftBar closed the pipe; nothing left to flush
        except Exception as exc:
            print(" | sfimage=exclamationmark.triangle color=#c92a2a")
            print("---")
            print("Time Tracker failed: %s" % str(exc).replace("|", "¦"))
            print("Open data folder | bash=/usr/bin/open param1=%s terminal=false"
                  % tt.HOME)
            print("~~~", flush=True)
