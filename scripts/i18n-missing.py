#!/usr/bin/env python3
"""List the translation keys each language is missing compared with English.

Covers the bot catalogs (djsnake/locales/{code}.json) and the clip page's
I18N dictionary (web/index.html). `en` and `fr` must be complete (the test
suite enforces it); `es` and `de` are best effort and fall back to English
for a missing key. Always exits 0: this is a report, not a check.

Usage: python3 scripts/i18n-missing.py [code ...]
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALES = os.path.join(ROOT, "djsnake", "locales")
PAGE = os.path.join(ROOT, "web", "index.html")


def load_catalogs():
    catalogs = {}
    for name in sorted(os.listdir(LOCALES)):
        if name.endswith(".json"):
            with open(os.path.join(LOCALES, name), encoding="utf-8") as f:
                catalogs[name[:-5]] = json.load(f)
    return catalogs


def load_page_dictionaries():
    with open(PAGE, encoding="utf-8") as f:
        page = f.read()
    result = {}
    for match in re.finditer(r"\n    (\w+): {\n(.*?)\n    }", page, re.S):
        keys = re.findall(r"^\s+(\w+): \"", match.group(2), re.M)
        if len(keys) > 50:
            result[match.group(1)] = keys
    return result


def report(title, reference, languages):
    print(f"{title} ({len(reference)} keys in English)")
    for code, keys in languages.items():
        missing = [k for k in reference if k not in keys]
        done = len(reference) - len(missing)
        print(f"  {code}: {done}/{len(reference)} ({100 * done / len(reference):.0f}%)")
        for key in missing:
            print(f"    - {key}")


def main(argv):
    wanted = set(argv)
    catalogs = load_catalogs()
    english = list(catalogs["en"])
    report(
        "Bot messages",
        english,
        {
            c: set(k)
            for c, k in catalogs.items()
            if c != "en" and (not wanted or c in wanted)
        },
    )
    pages = load_page_dictionaries()
    if "en" in pages:
        report(
            "Clip page",
            pages["en"],
            {
                c: set(k)
                for c, k in pages.items()
                if c != "en" and (not wanted or c in wanted)
            },
        )


if __name__ == "__main__":
    main(sys.argv[1:])
