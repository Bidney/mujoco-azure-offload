"""Render the cloud-init (customData) that bootstraps the VM and installs the
self-destruct watchdog.

The remote runner/worker/watchdog source files live in azoffload/remote/ and are
base64-embedded into write_files, so the VM is fully self-contained: it does not
need to fetch anything before it can deallocate itself.

Ordering matters for cost-safety: the watchdog is started FIRST (it only needs
curl + python3, present on the base image) so the absolute max-lifetime is
enforced even if apt/pip later hangs.
"""
import base64
import os
import shlex

REMOTE = os.path.join(os.path.dirname(__file__), "remote")


def _b64_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _b64_str(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def render(env_vars: dict) -> str:
    files = [
        ("/opt/mjoff/runner.py", _b64_file(os.path.join(REMOTE, "runner.py")), "0644"),
        ("/opt/mjoff/worker.py", _b64_file(os.path.join(REMOTE, "worker.py")), "0644"),
        ("/opt/mjoff/watchdog.sh", _b64_file(os.path.join(REMOTE, "watchdog.sh")), "0755"),
        # shlex.quote each value: the env file is sourced by bash (watchdog + the
        # runner launch), so an unquoted config value could otherwise inject shell.
        ("/opt/mjoff/env",
         _b64_str("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env_vars.items())),
         "0644"),
    ]
    wf = ""
    for path, content, perm in files:
        wf += (
            f"  - path: {path}\n"
            f"    permissions: '{perm}'\n"
            f"    encoding: b64\n"
            f"    content: {content}\n"
        )

    pkgs = "python3-venv python3-pip ffmpeg libegl1 libgles2 libosmesa6 curl ca-certificates"
    return f"""#cloud-config
write_files:
{wf}runcmd:
  - [ bash, -lc, "mkdir -p /opt/mjoff/out && touch /opt/mjoff/heartbeat" ]
  - [ bash, -lc, "nohup bash /opt/mjoff/watchdog.sh >/opt/mjoff/watchdog.log 2>&1 &" ]
  - [ bash, -lc, "DEBIAN_FRONTEND=noninteractive apt-get update -y || true" ]
  - [ bash, -lc, "DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs} || true" ]
  - [ bash, -lc, "python3 -m venv /opt/mjoff/venv && /opt/mjoff/venv/bin/pip install --upgrade pip" ]
  - [ bash, -lc, "/opt/mjoff/venv/bin/pip install azure-identity azure-storage-blob" ]
  - [ bash, -lc, "set -a; . /opt/mjoff/env; set +a; nohup /opt/mjoff/venv/bin/python /opt/mjoff/runner.py >/opt/mjoff/runner.boot.log 2>&1 &" ]
"""


def build_env(s, run_id, prefix, max_lifetime_sec, stuck_sec, nproc,
              identity_client_id="") -> dict:
    return {
        "MJOFF_ACCOUNT_URL": s.account_url,
        # empty for a system-assigned identity; set for a user-assigned one so the
        # runner (ManagedIdentityCredential) and watchdog (IMDS) pick the right one.
        "MJOFF_IDENTITY_CLIENT_ID": identity_client_id,
        "MJOFF_CONTAINER": s.container,
        "MJOFF_RUN_ID": run_id,
        "MJOFF_PREFIX": prefix,
        "MJOFF_NPROC": str(nproc),
        "MJOFF_MUJOCO_GL": s.mujoco_gl,
        "MJOFF_JOB_MODULE": s.job_module,
        "MJOFF_ENTRY": s.entry_function,
        "MJOFF_SCENARIOS": s.scenarios_file,
        "MJOFF_HEARTBEAT_FILE": "/opt/mjoff/heartbeat",
        "MJOFF_HEARTBEAT_INTERVAL": str(s.heartbeat_interval_sec),
        "MJOFF_RUNNER_STARTED": "/opt/mjoff/RUNNER_STARTED",
        "MJOFF_COMPLETE": "/opt/mjoff/COMPLETE",
        "MAX_LIFETIME_SECONDS": str(int(max_lifetime_sec)),
        "STUCK_TIMEOUT_SECONDS": str(int(stuck_sec)),
    }
