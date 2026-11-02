"""Turn raw captures (tests/fixtures/raw/, git-ignored) into committable fixtures.

Scrubs: snowflake ids → stable fake ids, interaction tokens, user/guild names, avatars,
CDN signatures (`hm`) and placeholders. Structure, sizes, types and filenames are kept.

Usage: python tests/spike/scrub_fixtures.py <raw-name>=<fixture-name> ...
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "tests" / "fixtures" / "raw"
OUT = ROOT / "tests" / "fixtures"

SNOWFLAKE = re.compile(r"(?<!\d)\d{17,20}(?!\d)")
HM = re.compile(r"hm=[0-9a-f]+")
NAME_KEYS = {"username", "global_name", "nick", "display_name"}
DROP_KEYS = {
    "avatar",
    "banner",
    "avatar_decoration_data",
    "clan",
    "primary_guild",
    "collectibles",
    "display_name_styles",
    "icon",
    "splash",
    "placeholder",
}


class Scrubber:
    def __init__(self) -> None:
        self.ids: dict[str, str] = {}

    def fake_id(self, real: str) -> str:
        # Keep ordering-free but stable and still snowflake-shaped.
        return self.ids.setdefault(real, str(100000000000000000 + len(self.ids) + 1))

    def text(self, s: str) -> str:
        s = SNOWFLAKE.sub(lambda m: self.fake_id(m.group()), s)
        return HM.sub("hm=" + "0" * 64, s)

    def walk(self, node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in DROP_KEYS:
                    out[k] = None
                elif k == "token":
                    out[k] = "REDACTED"
                elif k == "discriminator":
                    out[k] = "0000"
                elif k in {"joined_at", "premium_since"} and v:
                    out[k] = "2020-01-01T00:00:00+00:00"
                elif k in NAME_KEYS and isinstance(v, str):
                    out[k] = "user"
                elif k == "name" and key in {"guild", "channel"}:
                    out[k] = key
                else:
                    out[self.text(k)] = self.walk(v, k)
            return out
        if isinstance(node, list):
            return [self.walk(v, key) for v in node]
        if isinstance(node, str):
            return self.text(node)
        return node


def main() -> None:
    scrubber = Scrubber()  # shared, so ids stay consistent across fixtures
    for arg in sys.argv[1:]:
        raw, _, name = arg.partition("=")
        data = json.loads((RAW / raw).read_text(encoding="utf-8"))
        (OUT / name).write_text(json.dumps(scrubber.walk(data), indent=2) + "\n", encoding="utf-8")
        print(f"{raw} -> {name}")


if __name__ == "__main__":
    main()
