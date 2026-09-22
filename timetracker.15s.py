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
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(os.path.realpath(__file__)).parent))
import tt_core as tt

SELF = os.path.abspath(__file__)
RUN = "bash=%s terminal=false refresh=true" % SELF

TICK = 1.0
RELOAD = 15.0
BEAT = 30.0

MAX_TITLE = 18

GREY = "color=#888888"
RED = "color=#c92a2a"
ORANGE = "#e8590c"
MONO = ("font=Menlo", "size=12")


def esc(text):
    return str(text).replace("|", "¦").replace("\n", " ")


def shellish(value):
    value = str(value)
    return '"%s"' % value if (" " in value or not value) else value


def trunc(text, n=MAX_TITLE):
    text = str(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_printed = 0
_sep_pending = False


def line(text, *params, depth=0):
    global _printed, _sep_pending
    if _sep_pending and _printed:
        print("---")
    _sep_pending = False
    body = esc(text)
    parts = [p for p in params if p]
    print("--" * depth + (body + " | " + " ".join(parts) if parts else body))
    _printed += 1


def sep(depth=0):
    global _sep_pending
    if depth:
        print("--" * depth + "---")
    else:
        _sep_pending = True


def action(text, *args, icon=None, style=(), depth=0):
    params = [RUN] + ["param%d=%s" % (i + 1, shellish(a))
                      for i, a in enumerate(args)]
    if icon:
        params.append("sfimage=" + icon)
    line(text, *(params + list(style)), depth=depth)


def heading(text, *style, depth=0):
    action("**%s**" % text, "noop",
           style=["md=True", "size=14"] + list(style), depth=depth)


def subheading(text, *style, depth=0):
    line(text, "size=11", GREY, *style, depth=depth)


def dropdown(text, *style, icon=None, depth=0):
    action(text, "noop", icon=icon, style=style, depth=depth)


def alert(text, depth=0):
    line(text, "size=11", RED, depth=depth)


def menu_bar(st, task):
    if not task:
        line("", "sfimage=stopwatch", "size=13")
        return
    icon, color = (("record.circle.fill", ORANGE) if st.running
                   else ("pause.circle", "#888888"))
    line("%s  %s" % (trunc(task["name"]), tt.hms(tt.total_today(st, task["id"]))),
         "sfimage=" + icon, "sfcolor=" + color, "font=Menlo", "size=13")


def overview(st):
    heading("Time Tracker")
    since, until, _, _ = tt.period_bounds(0)
    if tt.minutes(st.grand_total(since, until)):
        subheading("Today  %s      Pay period  %s"
                   % (tt.short(st.grand_total(*tt.day_bounds())),
                      tt.short(st.grand_total(since, until))))
    sep()

    notice = tt.notice()
    if notice:
        alert("· %s" % trunc(notice, 44))
        sep()


def task_view(st, task):
    heading("%s  %s" % (task["name"], tt.hms(tt.total_today(st, task["id"]))))
    if st.running:
        since, until, _, _ = tt.period_bounds(0)
        subheading("Today: %s • Pay period %s"
                   % (tt.short(tt.total_today(st, task["id"])),
                      tt.short(st.total(task["id"], since, until))))
        sep()
        action("Pause", "pause", icon="pause.circle")
    else:
        subheading("paused at %s · %s today"
                   % (tt.local(st.paused["at"]).strftime("%H:%M"),
                      tt.short(tt.total_today(st, task["id"]))))
        sep()
        action("Resume", "start", task["id"], icon="play.circle")
    action("Stop", "stop", icon="stop.circle")
    sep()
    action("Finish", "complete", task["id"], icon="checkmark.circle")
    options_menu(task)


def root_view(st):
    tasks = st.active_tasks()
    if tasks:
        subheading("Tasks")
    for task in tasks:
        action("%-26s %8s" % (trunc(task["name"], 26),
                                 tt.short(st.total(task["id"]))),
               "start", task["id"], style=MONO, icon="play")
    sep()
    action("New task", "new", icon="plus.circle")
    manage_menu(tasks)
    completed_menu(st)

    sep()
    if st.closed_sessions():
        export_menu()
    config_menu()


def options_menu(task):
    dropdown("Options", icon="ellipsis.circle")
    action("Rename", "rename", task["id"], depth=1, icon="pencil")
    action("Reset current session", "reset", task["id"], depth=1,
           icon="arrow.counterclockwise")
    action("Delete", "delete", task["id"], depth=1, icon="trash")


def manage_menu(tasks):
    if not tasks:
        return
    dropdown("Manage", icon="ellipsis.circle")
    for task in tasks:
        dropdown(trunc(task["name"], 18), depth=1)
        action("Rename", "rename", task["id"], depth=2)
        action("Mark complete", "complete", task["id"], depth=2)
        action("Delete", "delete", task["id"], depth=2)


def completed_menu(st):
    done = st.completed_tasks()
    if not done:
        return
    dropdown("Completed (%d)" % len(done))
    for task in done[:25]:
        dropdown("%s — %s" % (trunc(task["name"], 26),
                              tt.short(st.total(task["id"]))), depth=1)
        action("Reopen & start", "start", task["id"], depth=2)
        action("Reopen only", "reopen", task["id"], depth=2)
        action("Delete", "delete", task["id"], depth=2)


def export_menu():
    dropdown("Export timesheet")
    for offset, name in ((0, "This pay period"), (-1, "Last pay period")):
        _, _, first, last = tt.period_bounds(offset)
        action("%s · %s" % (name, tt.label_range(first, last)),
               "export", "period", str(offset), depth=1)
    action("Custom dates", "export", "range", depth=1)
    action("Everything", "export", "all", depth=1)


def config_menu():
    dropdown("Config")
    _, _, first, last = tt.period_bounds(0)
    _, _, next_first, _ = tt.period_bounds(1)
    subheading("Pay period: %d days · Current: %s · Next: %s"
               % (tt.config()["period_days"], tt.label_range(first, last),
                  next_first.strftime("%-d %b")), depth=1)
    action("Change pay period", "set-period", depth=1)
    action("Open data folder", "open-folder", depth=1,
           icon="rectangle.portrait.and.arrow.right")
    action("Refresh", "noop", depth=1, icon="arrow.clockwise")


def render(st):
    global _printed, _sep_pending
    _printed, _sep_pending = 0, False

    task = st.current()
    menu_bar(st, task)
    sep()
    overview(st)
    if task:
        task_view(st, task)
    else:
        root_view(st)


def log_sig():
    try:
        s = tt.EVENTS.stat()
        return (s.st_mtime, s.st_size)
    except OSError:
        return None


def stream():
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
        time.sleep(TICK - (time.time() % TICK))


def do_export(st, rest):
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
    tt.reveal(written)


def do_set_period():
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
        do_export(st, rest)

    elif cmd == "set-period":
        do_set_period()

    elif cmd == "open-folder":
        tt.subprocess.run(["open", str(tt.HOME)])

    elif cmd == "toggle-gap":
        tt.set_config("close_on_gap", not tt.config()["close_on_gap"])

    elif cmd == "noop":
        pass

    else:
        tt.cli(argv)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            do(sys.argv[1:])
        except SystemExit:
            raise
        except Exception as exc:
            tt.flash("%s: %s" % (type(exc).__name__, exc))
            sys.exit(1)
    else:
        try:
            stream()
        except (KeyboardInterrupt, BrokenPipeError):
            os._exit(0)
        except Exception as exc:
            print(" | sfimage=exclamationmark.triangle color=#c92a2a")
            print("---")
            print("Time Tracker failed: %s" % str(exc).replace("|", "¦"))
            print("Open data folder | bash=/usr/bin/open param1=%s terminal=false"
                  % tt.HOME)
            print("~~~", flush=True)
