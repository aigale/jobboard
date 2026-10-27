"""Build static/i18n/tl.json and static/i18n/ceb.json from the translation tables.

Run from the project folder:  python i18n_src/build.py
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from part1_public import T as T1  # noqa: E402
from part2_seeker import T as T2  # noqa: E402
from part3_employer import T as T3  # noqa: E402
from patterns import P  # noqa: E402

norm = lambda s: re.sub(r"\s+", " ", s).strip()
merged = {}
for table in (T1, T2, T3):
    for en, pair in table.items():
        merged[norm(en)] = pair
out = HERE.parent / "static" / "i18n"
out.mkdir(exist_ok=True)
for i, lang in enumerate(("tl", "ceb")):
    data = {"lang": lang, "strings": {k: v[i] for k, v in sorted(merged.items())},
            "patterns": [[p[0], p[1 + i]] for p in P]}
    (out / f"{lang}.json").write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
print(f"{len(merged)} strings, {len(P)} patterns")
