from __future__ import annotations

import io
import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from zipbot import stats as stats_mod
from zipbot.config import Settings
from zipbot.stats import Stats, StatsFileError, fetch_install_counts, main, read_days

DAY = 24 * 3600
T0 = datetime(2026, 11, 4, 12, tzinfo=UTC).timestamp()
USER = 123456789012345678  # a Discord-sized id; must never reach the file


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_counts_per_day() -> None:
    s = Stats(clock=Clock())
    s.record("download_media")
    s.record("download_media")
    s.record("files_linked", 5)
    assert s.snapshot() == {"2026-11-04": {"download_media": 2, "files_linked": 5}}
    assert s.dirty


def test_distinct_users_per_day() -> None:
    clock = Clock()
    s = Stats(clock=clock)
    s.record("download_media", user_id=1)
    s.record("add_to_zip", user_id=1)
    s.record("zip_show", user_id=2)
    s.record("link_opens")  # web events carry no user
    assert s.days["2026-11-04"]["users"] == 2

    clock.t += DAY  # the next UTC day: the set starts over
    s.record("download_media", user_id=1)
    assert s.days["2026-11-05"]["users"] == 1
    assert s.days["2026-11-04"]["users"] == 2


def test_day_boundary_is_utc_midnight() -> None:
    clock = Clock(datetime(2026, 11, 4, 23, 59, 59, tzinfo=UTC).timestamp())
    s = Stats(clock=clock)
    s.record("revoke")
    clock.t += 1
    s.record("revoke")
    assert s.snapshot() == {"2026-11-04": {"revoke": 1}, "2026-11-05": {"revoke": 1}}


@pytest.mark.parametrize("name", ["users", "Bad", "has space", "a" * 41, "", "1abc", "x/y"])
def test_record_rejects_bad_names(name: str) -> None:
    with pytest.raises(ValueError):
        Stats().record(name)


def test_keeps_400_days() -> None:
    clock = Clock()
    s = Stats(clock=clock)
    for _ in range(405):
        s.record("dm_burst")
        clock.t += DAY
    days = sorted(s.days)
    assert len(days) == 400
    assert days[-1] == datetime.fromtimestamp(clock.t - DAY, UTC).date().isoformat()


def test_flush_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    clock = Clock()
    s = Stats(path, clock=clock)
    assert not s.flush()  # nothing to save yet
    s.record("download_media", user_id=USER)
    s.record("links_created")
    assert s.flush() and not s.dirty
    assert not s.flush()  # unchanged: no write
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {
        "version": 1,
        "days": {"2026-11-04": {"download_media": 1, "links_created": 1, "users": 1}},
    }

    again = Stats.load(path, clock=clock)
    assert again.snapshot() == s.snapshot()
    # After a restart the in-memory set is empty: the same user counts again (approximate).
    again.record("download_media", user_id=USER)
    assert again.days["2026-11-04"]["users"] == 2
    again.record("download_media", user_id=USER)
    assert again.days["2026-11-04"]["users"] == 2


def test_file_has_counters_only(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    s = Stats(path, clock=Clock())
    for name in ("download_media", "add_to_zip", "zip_show", "dm_burst", "revoke"):
        s.record(name, user_id=USER)
    s.record("link_opens")
    s.flush()
    text = path.read_text(encoding="utf-8")
    assert str(USER) not in text
    raw = json.loads(text)
    assert set(raw) == {"version", "days"}
    for day, counters in raw["days"].items():
        datetime.fromisoformat(day)
        assert all(isinstance(v, int) for v in counters.values())
        assert all(stats_mod.NAME_RE.fullmatch(k) for k in counters)


def test_flush_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "stats.json"
    s = Stats(path, clock=Clock())
    s.record("revoke")
    s.flush()
    before = path.read_text(encoding="utf-8")
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def failing_replace(src: Any, dst: Any) -> None:
        replaced.append((str(src), str(dst)))
        raise OSError("disk full")

    s.record("revoke")
    monkeypatch.setattr(stats_mod.os, "replace", failing_replace)
    assert not s.flush()
    ((src, dst),) = replaced
    assert Path(src).parent == tmp_path and dst == str(path)  # temp file in the same directory
    assert path.read_text(encoding="utf-8") == before  # the old file is untouched
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stats.json"]  # temp file cleaned up
    assert s.dirty  # retried next time

    monkeypatch.setattr(stats_mod.os, "replace", real_replace)
    assert s.flush()
    assert json.loads(path.read_text(encoding="utf-8"))["days"]["2026-11-04"]["revoke"] == 2


def test_flush_failure_logs_type_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    s = Stats(tmp_path / "missing-dir" / "stats.json", clock=Clock())
    s.record("revoke")
    with caplog.at_level(logging.WARNING):
        assert not s.flush()
    assert caplog.messages == ["saving stats failed: FileNotFoundError"]


def test_load_without_path_or_file(tmp_path: Path) -> None:
    assert Stats.load(None).path is None
    s = Stats.load(tmp_path / "stats.json")
    assert s.days == {} and not s.dirty
    assert not (tmp_path / "stats.json").exists()


def test_load_prunes_old_days(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text(
        json.dumps({"version": 1, "days": {"2024-01-01": {"revoke": 1}, "2026-11-03": {"revoke": 2}}})
    )
    s = Stats.load(path, clock=Clock())
    assert s.snapshot() == {"2026-11-03": {"revoke": 2}}
    assert s.dirty  # the next flush drops it from the file too


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 2, "days": {}}),
        json.dumps({"version": 1, "days": []}),
        json.dumps({"version": 1, "days": {"yesterday": {}}}),
        json.dumps({"version": 1, "days": {"2026-11-04": []}}),
        json.dumps({"version": 1, "days": {"2026-11-04": {"revoke": -1}}}),
        json.dumps({"version": 1, "days": {"2026-11-04": {"revoke": True}}}),
        json.dumps({"version": 1, "days": {"2026-11-04": {"user 1234": 1}}}),
    ],
)
def test_corrupt_file_is_moved_aside(tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "stats.json"
    path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        s = Stats.load(path, clock=Clock())
    assert s.days == {} and s.path == path
    assert (tmp_path / "stats.json.corrupt").read_text(encoding="utf-8") == content
    assert not path.exists()
    assert "moved to stats.json.corrupt" in caplog.text
    assert content not in caplog.text
    s.record("revoke")
    assert s.flush()
    assert (tmp_path / "stats.json.corrupt").read_text(encoding="utf-8") == content  # never overwritten


def test_undecodable_file_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(StatsFileError):
        read_days(path)


def test_corrupt_file_that_cannot_be_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "stats.json"
    path.write_text("{oops", encoding="utf-8")

    def no_replace(src: Any, dst: Any) -> None:
        raise PermissionError

    monkeypatch.setattr(stats_mod.os, "replace", no_replace)
    with caplog.at_level(logging.WARNING):
        s = Stats.load(path, clock=Clock())
    assert s.path is None  # memory only: the bad file is left for a human
    assert "PermissionError" in caplog.text
    s.record("revoke")
    assert not s.flush()
    assert path.read_text(encoding="utf-8") == "{oops"


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(StatsFileError, match=r"IsADirectoryError|PermissionError"):
        read_days(tmp_path)  # a directory: can't be read as a file


def test_settings_stats_path(settings: Settings) -> None:
    assert settings.stats_path is None
    assert settings.model_validate({**settings.model_dump(), "stats_path": ""}).stats_path is None
    s = settings.model_validate({**settings.model_dump(), "stats_path": "/data/stats.json"})
    assert s.stats_path == Path("/data/stats.json")


# --- the CLI ---------------------------------------------------------------------------------

TOKEN = "MTIz.secret-bot-token.xyz"
FIXTURE_DAYS = {
    "2026-11-04": {"users": 3, "download_media": 4, "links_created": 5, "files_linked": 17, "link_opens": 6},
    "2026-11-02": {"users": 1, "add_to_zip": 2, "zip_link": 1, "revoke": 1, "rate_limited": 2},
    "2026-10-20": {"users": 2, "dm_burst": 3, "links_created": 3, "files_linked": 9, "link_misses": 1},
    "2026-01-01": {"users": 5, "download_media": 10},
}


@pytest.fixture
def stats_file(tmp_path: Path) -> Path:
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"version": 1, "days": FIXTURE_DAYS}), encoding="utf-8")
    return path


def run_cli(argv: list[str], env: dict[str, str], fetch: Any = None) -> tuple[int, str]:
    out = io.StringIO()
    fetch = fetch or (lambda token: {"guilds": 12, "user_installs": 345})
    code = main(argv, environ=env, out=out, clock=Clock(), fetch=fetch)
    return code, out.getvalue()


def test_cli_table(stats_file: Path) -> None:
    seen: list[str | None] = []

    def fetch(token: str | None) -> dict[str, Any]:
        seen.append(token)
        return {"guilds": 12, "user_installs": 345}

    code, out = run_cli([], {"STATS_PATH": str(stats_file), "DISCORD_TOKEN": TOKEN}, fetch)
    assert code == 0 and seen == [TOKEN]
    lines = out.splitlines()
    header = lines[1].split()
    assert header[:4] == ["date", "users", "dl", "add"]
    rows = [line.split() for line in lines[2:32]]
    assert [r[0] for r in rows][:3] == ["2026-11-04", "2026-11-03", "2026-11-02"]  # newest first
    assert rows[1][1:] == ["0"] * (len(header) - 1)  # zero days are shown
    assert rows[-1][0] == "2026-10-06" and len(rows) == 30
    col = {h: i for i, h in enumerate(header)}
    totals = {
        " ".join(line.split()[:2]): line.split()
        for line in lines
        if line.startswith(("7 days", "30 days", "all time"))
    }
    assert totals["7 days"][col["users"] + 1] == "4"
    assert totals["30 days"][col["files"] + 1] == "26"
    assert totals["all time"][col["dl"] + 1] == "14"
    assert "Discord installs (approximate): 12 servers, 345 users" in out
    assert "last minute may not be included" in out
    assert TOKEN not in out


def test_cli_days_and_json(stats_file: Path) -> None:
    code, out = run_cli(["--days", "3", "--json"], {"STATS_PATH": str(stats_file)})
    assert code == 0
    report = json.loads(out)
    assert list(report["days"]) == ["2026-11-04", "2026-11-03", "2026-11-02"]
    assert report["days"]["2026-11-03"] == {}
    assert report["totals"]["7_days"]["link_opens"] == 6
    assert report["totals"]["all_time"]["download_media"] == 14
    assert report["installs"] == {"guilds": 12, "user_installs": 345}

    code, out = run_cli(["--days", "2"], {"STATS_PATH": str(stats_file)})
    assert sum(line[:4] == "2026" for line in out.splitlines()) == 2


def test_cli_without_stats_path_or_file(tmp_path: Path) -> None:
    code, out = run_cli([], {})
    assert code == 1 and "STATS_PATH is not set" in out
    code, out = run_cli([], {"STATS_PATH": str(tmp_path / "none.json")})
    assert code == 0 and "2026-11-04" in out  # no file yet: all zeros


def test_cli_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text("{", encoding="utf-8")
    code, out = run_cli([], {"STATS_PATH": str(path)})
    assert code == 1 and "Can't read the stats file: invalid JSON" in out
    assert path.read_text(encoding="utf-8") == "{"  # the CLI never moves or rewrites it


def test_cli_rejects_zero_days(stats_file: Path) -> None:
    with pytest.raises(SystemExit):
        run_cli(["--days", "0"], {"STATS_PATH": str(stats_file)})


def test_cli_install_counts_unavailable(stats_file: Path) -> None:
    def failing(token: str | None) -> dict[str, Any]:
        return fetch_install_counts(token, opener=boom)

    def boom(request: Any, timeout: float) -> Any:
        raise OSError(f"body with {TOKEN}")

    code, out = run_cli([], {"STATS_PATH": str(stats_file), "DISCORD_TOKEN": TOKEN}, failing)
    assert code == 0
    assert "install counts unavailable (OSError)" in out
    assert TOKEN not in out


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None: ...


def test_fetch_install_counts() -> None:
    calls: list[Any] = []

    def opener(request: Any, timeout: float) -> FakeResponse:
        calls.append((request, timeout))
        body = {"approximate_guild_count": 7, "approximate_user_install_count": 89, "name": "x"}
        return FakeResponse(json.dumps(body).encode())

    assert fetch_install_counts(TOKEN, opener=opener) == {"guilds": 7, "user_installs": 89}
    ((request, timeout),) = calls
    assert request.full_url == "https://discord.com/api/v10/applications/@me"
    assert request.get_header("Authorization") == f"Bot {TOKEN}"
    assert timeout <= 10


def test_fetch_install_counts_failures() -> None:
    assert fetch_install_counts(None) == {"error": "no DISCORD_TOKEN"}
    leaky = FakeResponse(json.dumps({"message": TOKEN}).encode())  # missing fields; body echoes the token
    result = fetch_install_counts(TOKEN, opener=lambda request, timeout: leaky)
    assert result == {"error": "KeyError"}
    assert TOKEN not in json.dumps(result)


def test_totals_window() -> None:
    days = {k: Counter(v) for k, v in FIXTURE_DAYS.items()}
    today = datetime(2026, 11, 4, tzinfo=UTC).date()
    assert stats_mod.totals(days, today, 1)["users"] == 3
    assert stats_mod.totals(days, today, None)["users"] == 11


async def test_saver_flushes_periodically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from zipbot import __main__ as entry

    monkeypatch.setattr(entry, "FLUSH_INTERVAL_SEC", 0.01)
    s = Stats(tmp_path / "stats.json", clock=Clock())
    s.record("revoke")
    task = asyncio.create_task(entry.stats_saver(s))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not s.dirty:
            break
    task.cancel()
    assert (tmp_path / "stats.json").exists() and not s.dirty
