#!/usr/bin/env python3
"""Regenerate (or --check) the SYNTHETIC pin files under fixtures/pins/. Stdlib only, offline.

The revisions are sha1 digests of a descriptive string, so they are 40 lowercase hex
characters by construction and do not refer to any commit on the Hugging Face Hub.
The file sha256 values are computed from fixtures/synthetic-files/.

  python3 examples/hf-revision-pins/fixture_gen.py          # write
  python3 examples/hf-revision-pins/fixture_gen.py --check  # exit 1 on drift
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "fixtures", "synthetic-files")
OUT = os.path.join(HERE, "fixtures", "pins")
DESCRIPTION = ("SYNTHETIC fixture. The repo ids and revisions are invented for tests; "
               "they are not real Hugging Face repos or commits. Never prefetch them.")


def fake_revision(label):
    return hashlib.sha1(("rrl hf-revision-pins synthetic " + label).encode("utf-8")).hexdigest()


def file_entry(pin_id, rel, role):
    path = os.path.join(SRC, pin_id, *rel.split("/"))
    with open(path, "rb") as fh:
        data = fh.read()
    return {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "role": role}


def valid_doc():
    return {
        "schema_version": "hf-revision-pins/v1",
        "data_kind": "synthetic",
        "description": DESCRIPTION,
        "pins": [
            {
                "id": "tiny-policy",
                "repo_id": "rrl-synthetic/tiny-policy-fixture",
                "kind": "model",
                "revision": fake_revision("tiny-policy r1"),
                "license": "apache-2.0",
                "gated": False,
                "files": [file_entry("tiny-policy", "config.json", "config"),
                          file_entry("tiny-policy", "weights.synthetic.txt", "weights")],
            },
            {
                "id": "tiny-episodes",
                "repo_id": "rrl-synthetic/tiny-episodes-fixture",
                "kind": "dataset",
                "revision": fake_revision("tiny-episodes r1"),
                "license": "cc-by-4.0",
                "files": [file_entry("tiny-episodes", "data/episodes.jsonl", "data"),
                          file_entry("tiny-episodes", "meta.json", "metadata")],
            },
        ],
    }


def _mutate(rev=None, license_=None, drop_files=False):
    doc = valid_doc()
    pin = doc["pins"][0]
    if rev is not None:
        pin["revision"] = rev
    if license_ is not None:
        pin["license"] = license_
    if drop_files:
        for p in doc["pins"]:
            del p["files"]
    return doc


def documents():
    """Return {relative path: (document, expected codes or None for valid)}."""
    v = valid_doc()
    short = v["pins"][0]["revision"][:7]
    return {
        "synthetic-valid.json": (v, None),
        "synthetic-no-hashes.json": (_mutate(drop_files=True), None),
        "invalid/mutable-main.json": (_mutate(rev="main"), ["REVISION_BRANCH"]),
        "invalid/short-sha.json": (_mutate(rev=short), ["REVISION_SHORT_SHA"]),
        "invalid/tag-ref.json": (_mutate(rev="v1.0"), ["REVISION_NOT_COMMIT"]),
        "invalid/pr-ref.json": (_mutate(rev="refs/pr/3"), ["REVISION_PR_REF"]),
        "invalid/license-unknown.json": (_mutate(license_="unknown"), ["LICENSE"]),
    }


def render(doc):
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def expected_codes():
    return {k: codes for k, (_, codes) in documents().items() if codes is not None}


def outputs():
    out = {k: render(d) for k, (d, _) in documents().items()}
    out["invalid/expected_codes.json"] = json.dumps(expected_codes(), indent=2, sort_keys=True) + "\n"
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    drift = []
    for rel, text in outputs().items():
        path = os.path.join(OUT, *rel.split("/"))
        if check:
            try:
                with open(path, encoding="utf-8", newline="") as fh:
                    if fh.read() != text:
                        drift.append(rel)
            except OSError:
                drift.append(rel)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
    if check:
        known = set(outputs())
        for root, _dirs, files in os.walk(OUT):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), OUT).replace(os.sep, "/")
                if rel not in known:
                    drift.append(rel + " (stray)")
        if drift:
            print("fixture drift: " + ", ".join(sorted(drift)))
            return 1
        print("fixtures up to date (%d files)" % len(known))
        return 0
    print("wrote %d files under %s" % (len(outputs()), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
