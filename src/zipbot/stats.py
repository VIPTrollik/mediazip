"""Aggregate usage counters per UTC day: how often each feature is used, nothing about who.

The file (STATS_PATH) holds only `{"version": 1, "days": {"YYYY-MM-DD": {"counter": n}}}`.
Counter names are fixed identifiers (checked on record and on load), so no user id, IP
address, token, URL or file name can end up in it.

`users` is the number of distinct Discord users who did something that day. The ids are kept
in memory only, for today, and never written anywhere; only the count is saved. After a
restart the set starts empty, so a user active both before and after it is counted twice that
day: the number is approximate.

`python -m zipbot.stats` prints the saved counters (run it in the container:
`docker compose exec zipbot python -m zipbot.stats`). The app's owners also get them in
Discord with /stats (zipbot.cogs.stats), rendered by `reply_texts()`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

log = logging.getLogger(__name__)

VERSION = 1
KEEP_DAYS = 400
FLUSH_INTERVAL_SEC = 60
USERS = "users"
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")

# The table's columns: (counter, header). Anything else in the file shows up in --json only.
COLUMNS: tuple[tuple[str, str], ...] = (
    (USERS, "users"),
    ("download_media", "dl"),
    ("add_to_zip", "add"),
    ("zip_show", "show"),
    ("zip_link", "link"),
    ("zip_clear", "clear"),
    ("dm_burst", "dm"),
    ("revoke", "revoke"),
    ("no_media", "nomedia"),
    ("links_created", "links"),
    ("files_linked", "files"),
    ("link_opens", "opens"),
    ("link_misses", "misses"),
    ("rate_limited", "429"),
)
LEGEND = """dl: Download media   add: Add to ZIP   show/link/clear: /zip subcommands   dm: DM bursts answered
nomedia: "nothing to download" replies   links/files: links created, files in them (ZIP links too)
opens/misses: download page lookups of a live link / an unknown or expired one   429: rate limited
users: distinct users that day, approximate (someone active before and after a restart counts
  twice); its totals add up days, so they are user-days, not people.
The bot saves every 60 seconds, so the last minute may not be included yet."""

DISCORD_APP_URL = "https://discord.com/api/v10/applications/@me"
DISCORD_TIMEOUT_SEC = 5
USER_AGENT = "DiscordBot (https://github.com/VIPTrollik/mediazip, 1)"


class StatsFileError(Exception):
    """The stats file can't be read or isn't a valid version-1 stats file."""


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def parse_days(raw: Any) -> dict[str, Counter[str]]:
    """The `days` of a decoded stats file, checked: dates, counter names, non-negative ints."""
    if not isinstance(raw, dict) or raw.get("version") != VERSION or not isinstance(raw.get("days"), dict):
        raise StatsFileError("not a version 1 stats file")
    days: dict[str, Counter[str]] = {}
    for day, counters in raw["days"].items():
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError):
            raise StatsFileError("bad date") from None
        if not isinstance(counters, dict):
            raise StatsFileError("bad day")
        for name, value in counters.items():
            if not NAME_RE.fullmatch(name) or type(value) is not int or value < 0:
                raise StatsFileError("bad counter")
        days[day] = Counter(counters)
    return days


def read_days(path: Path) -> dict[str, Counter[str]]:
    """The file's days; {} if it doesn't exist yet. Raises StatsFileError if it is unusable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise StatsFileError(type(exc).__name__) from None
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        raise StatsFileError("invalid JSON") from None
    return parse_days(raw)


class Stats:
    """Counters per UTC day. With a `path`, `load()` reads it and `flush()` saves it; without
    one, everything stays in memory."""

    def __init__(
        self, path: Path | None = None, *, clock: Callable[[], float] = time.time, keep_days: int = KEEP_DAYS
    ) -> None:
        self.path = path
        self.clock = clock
        self.keep_days = keep_days
        self.days: dict[str, Counter[str]] = {}
        self.dirty = False
        self._users: set[int] = set()  # today's distinct users: memory only, never saved
        self._users_day: str | None = None

    @classmethod
    def load(cls, path: Path | None, *, clock: Callable[[], float] = time.time) -> Stats:
        """Start from the saved counters. An unusable file is moved aside to `<name>.corrupt`
        (never overwritten) and counting starts fresh; the bot starts either way."""
        stats = cls(path, clock=clock)
        if path is None:
            return stats
        try:
            stats.days = read_days(path)
        except StatsFileError as exc:
            aside = path.with_name(path.name + ".corrupt")
            try:
                os.replace(path, aside)
            except OSError as move_exc:
                # Keep the bad file untouched: count in memory only until someone looks at it.
                log.warning(
                    "stats file unusable (%s) and can't be moved aside (%s); not saving stats",
                    exc,
                    type(move_exc).__name__,
                )
                stats.path = None
            else:
                log.warning("stats file unusable (%s); moved to %s, starting fresh", exc, aside.name)
        stats.prune()
        return stats

    def today(self) -> str:
        return utc_day(self.clock())

    def record(self, name: str, n: int = 1, *, user_id: int | None = None) -> None:
        """Count `n` of `name` today; with `user_id`, also count the user once a day."""
        if not NAME_RE.fullmatch(name) or name == USERS:
            raise ValueError(f"bad counter name {name!r}")
        day = self.today()
        counters = self.days.get(day)
        if counters is None:
            counters = self.days[day] = Counter()
            self.prune()
        counters[name] += n
        if user_id is not None:
            if self._users_day != day:
                self._users, self._users_day = set(), day
            if user_id not in self._users:
                self._users.add(user_id)
                counters[USERS] += 1
        self.dirty = True

    def prune(self) -> None:
        """Drop days older than `keep_days` (today included)."""
        oldest = (date.fromisoformat(self.today()) - timedelta(days=self.keep_days - 1)).isoformat()
        for day in [d for d in self.days if d < oldest]:
            del self.days[day]
            self.dirty = True

    def snapshot(self) -> dict[str, dict[str, int]]:
        """A copy of all days, oldest first, counters sorted by name."""
        return {day: dict(sorted(self.days[day].items())) for day in sorted(self.days)}

    def dumps(self) -> str:
        return json.dumps({"version": VERSION, "days": self.snapshot()}, indent=1, sort_keys=True)

    def flush(self) -> bool:
        """Save atomically (a temp file in the same directory, then rename) if anything changed.
        A failure is logged (error type only) and retried at the next flush."""
        if self.path is None or not self.dirty:
            return False
        self.prune()
        data = self.dumps()
        tmp: str | None = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("saving stats failed: %s", type(exc).__name__)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            return False
        self.dirty = False
        return True


# --- the `python -m zipbot.stats` report ----------------------------------------------------


def totals(days: Mapping[str, Mapping[str, int]], today: date, window: int | None) -> Counter[str]:
    """Sums over the last `window` days up to `today` (all days if None)."""
    first = None if window is None else (today - timedelta(days=window - 1)).isoformat()
    out: Counter[str] = Counter()
    for day, counters in days.items():
        if first is None or first <= day <= today.isoformat():
            out.update(counters)
    return out


def fetch_install_counts(
    token: str | None, opener: Callable[..., Any] = urllib.request.urlopen
) -> dict[str, Any]:
    """Discord's approximate install counts, or {"error": <error type>}. Never returns or
    raises anything that contains the token or the response body."""
    if not token:
        return {"error": "no DISCORD_TOKEN"}
    request = urllib.request.Request(
        DISCORD_APP_URL, headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    )
    try:
        with opener(request, timeout=DISCORD_TIMEOUT_SEC) as response:
            body = json.loads(response.read())
        return {
            "guilds": int(body["approximate_guild_count"]),
            "user_installs": int(body["approximate_user_install_count"]),
        }
    except Exception as exc:  # any failure: report its type only
        return {"error": type(exc).__name__}


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Columns padded to fit, the first left-aligned and the rest right-aligned; an empty row
    becomes a rule."""
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows if r)) for i in range(len(headers))]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(
            c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        )

    out = [line(headers)]
    out.extend(line(r) if r else "-" * len(out[0]) for r in rows)
    return "\n".join(out)


def render_table(days: Mapping[str, Mapping[str, int]], today: date, show_days: int) -> str:
    headers = ["date", *(h for _, h in COLUMNS)]
    rows: list[list[str]] = []
    for back in range(show_days):
        day = (today - timedelta(days=back)).isoformat()
        counters = days.get(day, {})
        rows.append([day, *(str(counters.get(k, 0)) for k, _ in COLUMNS)])
    rows.append([])
    for label, window in (("7 days", 7), ("30 days", 30), ("all time", None)):
        sums = totals(days, today, window)
        rows.append([label, *(str(sums.get(k, 0)) for k, _ in COLUMNS)])
    return format_table(headers, rows)


def install_line(installs: Mapping[str, Any]) -> str:
    if "error" in installs:
        return f"Discord installs: install counts unavailable ({installs['error']})"
    users = installs.get("user_installs")
    users_text = "?" if users is None else users
    return f"Discord installs (approximate): {installs['guilds']} servers, {users_text} users"


# --- the /stats reply (zipbot.cogs.stats) -----------------------------------------------------

# Discord's limit on the text of all of a Components V2 message's text displays together,
# with a margin.
REPLY_TEXT_MAX = 3900
REPLY_MAX_DAYS = 90
ZIP_COUNTERS = ("zip_show", "zip_link", "zip_clear")
# The summary's rows: (label, counters added up).
SUMMARY_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("users", (USERS,)),
    ("Download media", ("download_media",)),
    ("Add to ZIP", ("add_to_zip",)),
    ("/zip", ZIP_COUNTERS),
    ("DM bursts", ("dm_burst",)),
    ("links created", ("links_created",)),
    ("files", ("files_linked",)),
    ("link opens", ("link_opens",)),
    ("misses", ("link_misses",)),
    ("429s", ("rate_limited",)),
)
SUMMARY_WINDOWS: tuple[tuple[str, int | None], ...] = (
    ("today", 1),
    ("7 days", 7),
    ("30 days", 30),
    ("all time", None),
)
# The per-day table's columns: (header, counters added up).
DAILY_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("users", (USERS,)),
    ("dl", ("download_media",)),
    ("add", ("add_to_zip",)),
    ("zip", ZIP_COUNTERS),
    ("dm", ("dm_burst",)),
    ("links", ("links_created",)),
    ("files", ("files_linked",)),
    ("opens", ("link_opens",)),
)
REPLY_HEAD = "📊 **MediaZip usage** (UTC days, live counters)"
DAILY_LEGEND = "-# dl: Download media, add: Add to ZIP, zip: /zip, dm: DM bursts, opens: link opens"
REPLY_FOOTER = (
    "-# users: distinct users per day, approximate (someone active before and after a restart "
    "counts twice that day). Totals over several days add up days, so they are user-days, not "
    "people. Install counts are Discord's own approximations."
)


def _sum(counters: Mapping[str, int], names: Sequence[str]) -> int:
    return sum(counters.get(n, 0) for n in names)


def code_block(text: str) -> str:
    return f"```\n{text}\n```"


def summary_table(days: Mapping[str, Mapping[str, int]], today: date) -> str:
    sums = [totals(days, today, window) for _, window in SUMMARY_WINDOWS]
    rows = [[label, *(str(_sum(s, names)) for s in sums)] for label, names in SUMMARY_ROWS]
    return format_table(["", *(label for label, _ in SUMMARY_WINDOWS)], rows)


def daily_table(days: Mapping[str, Mapping[str, int]], today: date, show_days: int) -> str:
    rows: list[list[str]] = []
    for back in range(show_days):
        day = today - timedelta(days=back)
        counters = days.get(day.isoformat(), {})
        rows.append([day.strftime("%m-%d"), *(str(_sum(counters, names)) for _, names in DAILY_COLUMNS)])
    return format_table(["day", *(h for h, _ in DAILY_COLUMNS)], rows)


def reply_texts(
    days: Mapping[str, Mapping[str, int]],
    today: date,
    installs: Mapping[str, Any],
    show_days: int,
    *,
    limit: int = REPLY_TEXT_MAX,
) -> tuple[list[str], list[str]]:
    """The /stats reply's text: (summary parts, per-day parts). The per-day table lists the
    newest `show_days` days (1 to REPLY_MAX_DAYS), fewer if they wouldn't fit in `limit`
    characters in all; then a note says how many are shown."""
    show_days = max(1, min(show_days, REPLY_MAX_DAYS))
    summary = [REPLY_HEAD, code_block(summary_table(days, today)), install_line(installs)]
    fixed = sum(map(len, summary))
    daily: list[str] = []
    for n in range(show_days, 0, -1):
        head = "**Today**" if n == 1 else f"**Last {n} days**"
        if n < show_days:
            head += f" (of {show_days} asked; more would not fit in one Discord message)"
        daily = [head, code_block(daily_table(days, today, n)), DAILY_LEGEND, REPLY_FOOTER]
        if fixed + sum(map(len, daily)) <= limit:
            break
    return summary, daily


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    out: TextIO | None = None,
    clock: Callable[[], float] = time.time,
    fetch: Callable[[str | None], dict[str, Any]] = fetch_install_counts,
) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="python -m zipbot.stats", description="Print MediaZip usage counters."
    )
    parser.add_argument("--days", type=int, default=30, help="days to list (default 30)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    path = environ.get("STATS_PATH", "").strip()
    if not path:
        print("STATS_PATH is not set: the bot keeps its counters in memory only.", file=out)
        return 1
    try:
        days = read_days(Path(path))
    except StatsFileError as exc:
        print(f"Can't read the stats file: {exc}", file=out)
        return 1
    today = datetime.fromtimestamp(clock(), UTC).date()
    installs = fetch(environ.get("DISCORD_TOKEN", "").strip() or None)

    if args.json:
        listed = [(today - timedelta(days=b)).isoformat() for b in range(args.days)]
        report = {
            "today": today.isoformat(),
            "days": {d: dict(sorted(days.get(d, {}).items())) for d in listed},
            "totals": {
                label: dict(sorted(totals(days, today, w).items()))
                for label, w in (("7_days", 7), ("30_days", 30), ("all_time", None))
            },
            "installs": installs,
        }
        print(json.dumps(report, indent=2), file=out)
        return 0

    print(f"MediaZip usage, UTC days, newest first (stats file: {path})", file=out)
    print(render_table(days, today, args.days), file=out)
    print(LEGEND, file=out)
    print(install_line(installs), file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
