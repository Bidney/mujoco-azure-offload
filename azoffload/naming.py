"""Deterministic, tag-able names for every resource this tool creates.

Every run gets a unique run-id. All Azure resources are tagged with it so the
teardown / --teardown-only sweep can always find and remove them, and all blobs
live under a runs/<run-id>/ prefix so they are equally discoverable.
"""
import datetime
import secrets

TOOL_TAG = "mujoco-azure-offload"
RUN_ID_TAG = "mjoff-run-id"
TOOL_TAG_KEY = "mjoff-tool"


def _utcnow_compact() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    """e.g. mjoff-20260627-141233-a1b2 — short enough for all Azure name limits."""
    return f"mjoff-{_utcnow_compact()}-{secrets.token_hex(2)}"


def resource_names(run_id: str) -> dict:
    """All resources derive from the run-id so teardown can reconstruct them
    even with no local state (e.g. after the laptop died)."""
    return {
        "vm": run_id,
        "ip": f"{run_id}-ip",
        "nsg": f"{run_id}-nsg",
        "disk": f"{run_id}-osdisk",
    }


def tags(run_id: str, owner: str = "") -> dict:
    return {
        RUN_ID_TAG: run_id,
        TOOL_TAG_KEY: TOOL_TAG,
        "mjoff-created-at": _utcnow_iso(),
        "mjoff-owner": owner or "unknown",
    }


def blob_prefix(run_id: str) -> str:
    return f"runs/{run_id}"
