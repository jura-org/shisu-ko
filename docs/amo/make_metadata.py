#!/usr/bin/env python3
"""Build docs/amo/amo-metadata.json, the listing metadata web-ext sign --channel listed sends to
addons.mozilla.org when a release is published on the listing (.github/workflows/amo-listing.yml,
publish-addon.cmd), from the text files next to this script and addon/manifest.json. <version> in
the reviewer notes is the release's own number, the one its tag carries, not the listed build's.

The privacy policy, the icon and the screenshots cannot be set through this file; they live in
the AMO Developer Hub (see README.md in this folder). Standard library only.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT = HERE / "amo-metadata.json"

HOMEPAGE = "https://github.com/Multysquid/shisu-ko"
SUPPORT_URL = "https://github.com/Multysquid/shisu-ko/issues"
CATEGORIES = ["language-support", "photos-music-videos"]  # at most two, slugs from /api/v5/addons/categories/
TAGS = ["youtube", "streaming"]  # AMO's fixed tag vocabulary; unknown tags are rejected
LICENSE = "MIT"  # SPDX-style slug of a built-in AMO license; must match LICENSE in the repo
NOTES_LIMIT = 3000  # release notes and approval notes, each (AMO's version serializer)


def read(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8").strip()


def fail(msg: str) -> None:
    sys.exit(f"make_metadata: {msg}")


def main() -> None:
    manifest = json.loads((ROOT / "addon" / "manifest.json").read_text(encoding="utf-8"))
    name, version = manifest["name"], manifest["version"]
    summary = read("summary.txt")
    description = read("description.md")
    release_notes = read("release-notes.md")
    approval_notes = read("reviewer-notes.md").replace("<version>", version)

    if len(name) > 50:
        fail("the name is longer than AMO's 50 characters")
    if len(summary) > 250:
        fail(f"summary.txt is {len(summary)} characters, AMO allows 250")
    if re.search(r"https?://|www\.", summary):
        fail("summary.txt must not contain URLs")
    # Any release may be published on the listing (amo-listing.yml, by hand, from its tag) with
    # these notes, so a version bump without new notes would publish the previous release's text;
    # CI runs this on every push.
    if not re.search(rf"(?<![\d.]){re.escape(version)}(?!\.?\d)", release_notes):
        fail(f"release-notes.md does not mention {version}, the version in addon/manifest.json")
    if len(description) > 15000:
        fail("description.md is longer than AMO's 15000 characters")
    # AMO refuses a version whose notes run past 3000 characters ("Ensure this field has no more
    # than 3000 characters"), and only when a release is submitted to the listing, long after its
    # tag, so it is checked here, on every push. The full reviewer guide is reviewer-guide.md,
    # linked from the notes.
    if len(release_notes) > NOTES_LIMIT:
        fail(f"release-notes.md is {len(release_notes)} characters, AMO allows {NOTES_LIMIT}")
    if len(approval_notes) > NOTES_LIMIT:
        fail(f"reviewer-notes.md is {len(approval_notes)} characters with the version filled in, AMO allows {NOTES_LIMIT}; the rest belongs in reviewer-guide.md")

    metadata = {
        "name": {"en-US": name},
        "summary": {"en-US": summary},
        "description": {"en-US": description},
        "categories": CATEGORIES,
        "tags": TAGS,
        "homepage": {"en-US": HOMEPAGE},
        "support_url": {"en-US": SUPPORT_URL},
        "is_experimental": False,
        "requires_payment": False,
        "default_locale": "en-US",
        "version": {
            "license": LICENSE,
            "compatibility": ["firefox"],
            "release_notes": {"en-US": release_notes},
            "approval_notes": approval_notes,
        },
    }
    OUT.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} for {name} {version} ({len(summary)}-character summary)")


if __name__ == "__main__":
    main()
