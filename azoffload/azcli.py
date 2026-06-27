"""Thin wrappers around the `az` CLI.

We use `az` for VM + networking + identity lifecycle (one `az vm create` builds
NIC/IP/NSG/disk and wires the managed identity, custom-data and tags — far less
code than the mgmt SDKs) and reuse the existing `az login` credential. Data-plane
blob access uses the SDK (see storage.py).
"""
import json
import shutil
import subprocess


class AzError(Exception):
    pass


def _az() -> str:
    exe = shutil.which("az")
    if not exe:
        raise AzError("Azure CLI 'az' not found on PATH. Install it and run 'az login' first.")
    return exe


def run(args, check=True):
    p = subprocess.run([_az()] + args, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AzError(f"az {' '.join(args)} failed (rc={p.returncode}): {p.stderr.strip()}")
    return p


def json_out(args):
    p = run(args + ["-o", "json"])
    out = p.stdout.strip()
    return json.loads(out) if out else None


def safe_json(args, default=None):
    """Best-effort JSON; never raises (used by idempotent teardown sweeps)."""
    try:
        p = run(args + ["-o", "json"], check=False)
        if p.returncode != 0 or not p.stdout.strip():
            return default
        return json.loads(p.stdout)
    except Exception:
        return default


def try_run(args):
    """Run, never raise. Returns (returncode, stderr)."""
    try:
        p = run(args, check=False)
        return p.returncode, (p.stderr or "")
    except AzError as e:
        return 1, str(e)
