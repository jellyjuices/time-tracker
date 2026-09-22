# Time Tracker

A SwiftBar menu-bar time tracker. One task runs at a time, and everything lands
in an append-only log that survives a hard shutdown.

```
./install.sh
```

Installs the plugin into your SwiftBar plugin folder (read from SwiftBar's own
prefs) and drops a `tt` CLI in `~/.local/bin`. `./uninstall.sh` reverses it and
leaves your data alone.

## Menu bar

| state   | icon                                                    |
| ------- | ------------------------------------------------------- |
| idle    | grey stopwatch                                          |
| running | orange record dot + name + `0:12:34`, ticking every second |
| paused  | grey pause icon + name + the same clock, held still     |

The clock is **today's time on that task**, not the current session, so it
keeps counting where it left off after a pause rather than restarting.

It runs as a SwiftBar [streamable plugin](https://github.com/swiftbar/SwiftBar):
one long-lived process prints a new menu every second, and only re-reads the
event log when the log has actually changed.

**While a task is current** the menu is only about that task — today and
this week totals, then:

- **Pause** / **Resume** — stop the clock, keep the task current
- **Stop** — close the session and drop the task as current
- **Finish** — complete it, which stops the clock in the same action
- **Options ▸** — rename it, reset its clock to zero, or delete it

Nothing else is in the way, so there is no ambiguity about what is being
counted. Stop, and the full menu comes back:

- the task list — click any task to start it
- **New task…** — asks for a name and starts tracking immediately
- **Manage ▸** — rename, complete or delete any open task
- **Completed (n) ▸** — finished tasks, with *Reopen & start* if you were wrong
- **Export timesheet ▸** — this pay period, last pay period, custom dates
  or everything

Completing a task removes it from the start list — it lives under *Completed*
from then on.

Nothing the tracker does raises a notification. Anything worth saying — a
recovered session, a rejected date, a failed click — appears as a red line in
the menu itself and clears after twenty seconds. *Reset* and *Delete* ask
first, since both throw work away.

## Data

Everything lives in `~/.time-tracker` (override with `$TIME_TRACKER_HOME`):

```
events.jsonl   append-only log — the source of truth
heartbeat      last moment the tracker was known alive
config.json    settings
exports/       default CSV destination
```

`events.jsonl` is written one line at a time, `flush` + `fsync` on every write,
under an `flock`. State is rebuilt by replaying the log, so:

- pulling the power loses at most the event being written at that instant
- a torn final line is skipped on read, and the next write starts a fresh line
- ten simultaneous menu clicks cannot interleave into a corrupt record

### Sleep and crashes

While a task runs, the plugin touches `heartbeat` every 30 seconds. If the tracker comes
back and the heartbeat is more than 5 minutes stale — laptop slept, SwiftBar
quit, machine died — the open session is closed at the last known-alive moment
rather than billing the whole gap to the task. The menu shows a one-line
`⚠︎ recovered` notice and the CSV marks those rows `recovered=yes`.

## CSV

One export: a **timesheet**, a day at a time. Each task gets a row, then the
day closes with a total row.

```
DATE,TASK NAME,HOURS
09/21,Client work,4
09/21,Internal,4
,,8
09/22,Client work,3.5
,,3.5
09/23,Client work,5
09/23,Internal,4
09/23,Admin,1.5
,,10.5
```

Hours round **up to the next half hour** per task per day — the grain that
actually goes on a sheet — and print without trailing zeroes (`4`, not `4.0`).
The total row carries no date or task name, so it reads as a subtotal.

Days are never padded or capped against the 8-hour day: a short day totals
`3.5`, an overrun totals `10.5`. Only days with work appear.

Work that runs past midnight is **split at midnight** and lands on the day it
was really done — 22:30 to 01:15 gives 1.5h to one day and 1.5h to the next.

### Windows

Every export takes a window: **this pay period**, **last pay period**, a custom
pair of dates, or everything. Both ends are whole days and both are inclusive,
so `--from 2026-09-07 --to 2026-09-20` covers 7 Sep 00:00 up to the end of
20 Sep. The window lands in the filename — `timesheet-2026-09-07_2026-09-20.csv`.

Set your pay period once under **Config ▸ Change pay period** (length in days,
plus any one date a period starts on); every window is counted forward and back
from that anchor, and the menu shows the resulting dates so you can check them.

A currently running session is included, clipped to now.

Nothing anywhere is reported in seconds; displays floor to whole minutes.

## CLI

Same data, same lock — safe to use while the menu is open.

```
tt status                       what's running
tt list [--all]                 tasks (--all includes completed)
tt new "Write proposal"         create and start
tt add "Later thing"            create without starting
tt start proposal               start / switch (id or name substring)
tt stop
tt done [proposal]              complete; defaults to the current task
tt reopen proposal
tt rename proposal "New name"
tt reset [proposal]             forget all its time, clock back to zero
tt pause / tt resume            hold the clock without losing the task
tt rm proposal
tt export --period                    this pay period
tt export --period 1                  the previous one
tt export --from 2026-09-07 --to 2026-09-20
tt export --days 14                   the last 14 days
tt export --all [path]                everything
tt period 14 2026-09-07               set length + anchor, print the dates
tt log [n]                      recent sessions
```

## Layout

| file                | what it is                                            |
| ------------------- | ----------------------------------------------------- |
| `tt_core.py`        | storage, event replay, actions, CSV export, CLI        |
| `timetracker.15s.py` | SwiftBar plugin: renders the menu, dispatches clicks   |
| `install.sh`        | symlinks both into place                               |

The plugin re-invokes itself for every click (`param1=start param2=<id>`), so
there is one code path for the menu and the shell.
