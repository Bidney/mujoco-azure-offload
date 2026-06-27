#!/usr/bin/env python3
"""Runs ONE scenario in the job venv. Imports the user's job module and calls
the entry function `run_scenario(scenario: dict, workdir: str) -> dict`.

Writes the returned summary to <outdir>/_result.json. Any artifacts (.json,
.png, .mp4, logs) the function drops in <outdir> are collected into the results
archive. Exit code != 0 marks the scenario failed (the runner retries nothing;
failures are surfaced in progress.recent_errors).
"""
import argparse
import importlib
import json
import os
import sys
import traceback


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    with open(a.scenario) as f:
        scenario = json.load(f)

    mod_name = os.environ.get("MJOFF_JOB_MODULE", "job")
    entry_name = os.environ.get("MJOFF_ENTRY", "run_scenario")
    mod = importlib.import_module(mod_name)
    entry = getattr(mod, entry_name)

    result = entry(scenario, a.outdir)
    with open(os.path.join(a.outdir, "_result.json"), "w") as f:
        json.dump(result if result is not None else {}, f, default=str, indent=2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
