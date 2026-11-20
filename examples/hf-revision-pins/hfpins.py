#!/usr/bin/env python3
"""Command line for hf-revision-pins/v1. Offline, stdlib only.

Exit codes: 0 = ok, 1 = invalid pin file or failed cache check, 2 = usage or I/O error.

  hfpins.py validate PINFILE... [--release]
  hfpins.py resolve  PINFILE [--cache-dir DIR] [--json]
  hfpins.py check    PINFILE [--cache-dir DIR] [--release] [--require-offline-env]
  hfpins.py seed-synthetic PINFILE --source DIR --cache-dir DIR [--copy]
  hfpins.py --list-codes
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import hf_cache  # noqa: E402
import hf_pins  # noqa: E402


def _load_valid(path, release, out):
    result = hf_pins.validate_file(path, release=release)
    if not result.ok:
        print("FAIL %r" % path, file=out)
        for d in result.diagnostics:
            print("  " + d.render(), file=out)
        return None
    return result.document


def cmd_validate(args):
    bad = 0
    for path in args.pinfiles:
        doc = _load_valid(path, args.release, sys.stdout)
        if doc is None:
            bad += 1
        else:
            print("OK   %r (%d pin(s), data_kind=%s%s)"
                  % (path, len(doc["pins"]), doc["data_kind"], ", release" if args.release else ""))
    mode = "release" if args.release else "default"
    print("%d valid, %d invalid (%s, %s mode)" % (len(args.pinfiles) - bad, bad, hf_pins.SCHEMA_VERSION, mode))
    return 1 if bad else 0


def cmd_resolve(args):
    doc = _load_valid(args.pinfile, False, sys.stdout)
    if doc is None:
        return 1
    cache_dir = hf_cache.resolve_cache_dir(args.cache_dir)
    resolved = hf_cache.resolve_document(doc, cache_dir)
    if args.json:
        payload = {"cache_dir": cache_dir, "pins": [
            {"id": r.pin_id, "kind": r.kind, "repo_id": r.repo_id, "revision": r.revision,
             "repo_dir": r.repo_dir, "snapshot_dir": r.snapshot_dir,
             "files": [{"path": rel, "cache_path": ap} for rel, ap in r.files]} for r in resolved]}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("cache_dir: %s" % cache_dir)
    for r in resolved:
        print("%s  %s %s@%s" % (r.pin_id, r.kind, r.repo_id, r.revision))
        print("  snapshot: %s" % r.snapshot_dir)
        for rel, ap in r.files:
            print("  file: %s -> %s" % (rel, ap))
    return 0


def cmd_check(args):
    result = hf_pins.validate_file(args.pinfile, release=args.release)
    cache_dir = hf_cache.resolve_cache_dir(args.cache_dir)
    if not result.ok:
        print("FAIL %r: pin file invalid, cache not inspected" % args.pinfile)
        for d in result.diagnostics:
            print("  " + d.render())
        return 1
    report = hf_cache.check_document(result.document, cache_dir, release=args.release,
                                     require_offline_env=args.require_offline_env)
    for f in report.findings:
        print(f.render())
    status = "OK" if report.ok else "FAIL"
    print("%s %r against %s: %d pin(s), %d file(s) verified by sha256, %d error(s), %d warning(s)"
          % (status, args.pinfile, cache_dir, report.pins_checked, report.verified_files,
             len(report.errors), len(report.findings) - len(report.errors)))
    return 0 if report.ok else 1


def cmd_seed(args):
    doc = _load_valid(args.pinfile, False, sys.stderr)
    if doc is None:
        return 1
    cache_dir = os.path.abspath(args.cache_dir)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        created = hf_cache.seed_synthetic_cache(doc, args.source, cache_dir, copy=args.copy)
    except (hf_cache.SeedError, OSError) as exc:
        print("seed refused: %s" % exc, file=sys.stderr)
        return 2
    for d in created:
        print("created %s" % d)
    print("seeded %d SYNTHETIC repo folder(s) under %s" % (len(created), cache_dir))
    return 0


def cmd_codes():
    print("Pin-file validator codes (%s):" % hf_pins.SCHEMA_VERSION)
    for k, v in hf_pins.CODES.items():
        print("  %-24s %s" % (k, v))
    print("Cache-check codes:")
    for k, v in hf_cache.CHECK_CODES.items():
        print("  %-24s %s" % (k, v))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="hfpins.py",
                                description="Offline checks for immutable Hugging Face revision pins.")
    p.add_argument("--list-codes", action="store_true", help="print every diagnostic code and exit")
    sub = p.add_subparsers(dest="cmd")

    v = sub.add_parser("validate", help="validate pin files (no filesystem cache access)")
    v.add_argument("pinfiles", nargs="+")
    v.add_argument("--release", action="store_true", help="also require file sha256s and data_kind 'real'")

    r = sub.add_parser("resolve", help="print the cache paths a pin file maps to")
    r.add_argument("pinfile")
    r.add_argument("--cache-dir", help="hub cache dir (default: HF_HUB_CACHE / HF_HOME/hub / ~/.cache/huggingface/hub)")
    r.add_argument("--json", action="store_true")

    c = sub.add_parser("check", help="verify the cache holds every pinned revision and file sha256")
    c.add_argument("pinfile")
    c.add_argument("--cache-dir")
    c.add_argument("--release", action="store_true", help="release-grade: file hashes required, synthetic rejected")
    c.add_argument("--require-offline-env", action="store_true", help="fail unless HF_HUB_OFFLINE is true")

    s = sub.add_parser("seed-synthetic", help="build a cache from SYNTHETIC fixture files (demo/tests only)")
    s.add_argument("pinfile")
    s.add_argument("--source", required=True, help="directory holding <pin id>/<path> fixture files")
    s.add_argument("--cache-dir", required=True)
    s.add_argument("--copy", action="store_true", help="copy files into snapshots instead of symlinking")
    return p


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.list_codes:
        return cmd_codes()
    handlers = {"validate": cmd_validate, "resolve": cmd_resolve, "check": cmd_check, "seed-synthetic": cmd_seed}
    if args.cmd not in handlers:
        parser.print_usage(sys.stderr)
        return 2
    paths = args.pinfiles if args.cmd == "validate" else [args.pinfile]
    unreadable = [p for p in paths if not os.path.isfile(p)]
    if unreadable:
        for p in unreadable:
            print("error: pin file %r does not exist or is not a file" % p, file=sys.stderr)
        return 2
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
