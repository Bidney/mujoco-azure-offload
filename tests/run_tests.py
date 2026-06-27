#!/usr/bin/env python3
"""Offline test suite for mujoco-azure-offload.

Covers: naming, config, pricing selection/fallback, cloud-init rendering +
shell-injection hardening, safe tar extraction, the monitor abort/ETA state
machine, controller blob Store round-trip, teardown idempotency, and a full
end-to-end run of the REAL on-VM runner.py driving the MuJoCo sweep in parallel
against a filesystem-backed fake Azure.

Run:  python3 tests/run_tests.py
"""
import base64
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
FAKE = os.path.join(HERE, "fakeazure")
sys.path.insert(0, FAKE)   # fake 'azure' (real one isn't installed)
sys.path.insert(0, PROJ)   # azoffload package

os.environ.setdefault("BLOB_ROOT", tempfile.mkdtemp(prefix="mjoff-blobroot-"))

import yaml  # noqa: E402

from azoffload import cloudinit, config, monitor, naming, pricing, storage, teardown  # noqa: E402

RESULTS = []


def test(fn):
    RESULTS.append(fn)
    return fn


def _eq(a, b, msg=""):
    assert a == b, f"{msg} expected {b!r} got {a!r}"


# ----------------------------------------------------------------------------
@test
def naming_formats():
    import re
    rid = naming.new_run_id()
    assert re.fullmatch(r"mjoff-\d{8}-\d{6}-[0-9a-f]{4}", rid), rid
    n = naming.resource_names(rid)
    _eq(n["ip"], rid + "-ip")
    _eq(n["disk"], rid + "-osdisk")
    t = naming.tags(rid, owner="me")
    _eq(t[naming.RUN_ID_TAG], rid)
    _eq(t[naming.TOOL_TAG_KEY], naming.TOOL_TAG)
    _eq(t["mjoff-owner"], "me")
    _eq(naming.blob_prefix(rid), "runs/" + rid)


@test
def config_load_override_validate():
    cfgp = os.path.join(tempfile.mkdtemp(), "config.yaml")
    with open(cfgp, "w") as f:
        f.write(yaml.safe_dump({
            "azure": {"resource_group": "rg", "region": "eastus"},
            "storage": {"account": "acct"},
            "compute": {"spot": True, "managed_identity": "/sub/x/id"},
            "limits": {"max_budget_usd": 5, "max_wall_clock_min": 60},
        }))
    s = config.load(cfgp)
    _eq(s.resource_group, "rg")
    _eq(s.spot, True)
    _eq(config.validate_for_run(s), [], "full config should validate clean")

    # overrides: --force-dedicated flips spot; --max-budget wins
    args = types.SimpleNamespace(
        subscription=None, resource_group=None, region=None, account=None,
        container=None, vm_size=None, nproc=None, managed_identity=None,
        max_budget=12.5, max_wall_clock=None, stuck_timeout=None, force_dedicated=True)
    s2 = config.apply_overrides(config.load(cfgp), args)
    _eq(s2.spot, False, "force_dedicated must clear spot")
    _eq(s2.max_budget_usd, 12.5)

    # missing required fields -> problems reported; identity is NOT required anymore
    bad = config.Settings()
    probs = config.validate_for_run(bad)
    assert any("resource_group" in p for p in probs)
    assert any("account" in p for p in probs)
    assert not any("managed_identity" in p for p in probs), "identity must be optional now"


@test
def tier_selection():
    base = dict(subscription=None, resource_group=None, region=None, account=None,
                container=None, nproc=None, managed_identity=None, max_budget=None,
                max_wall_clock=None, stuck_timeout=None, force_dedicated=False)
    s = config.apply_overrides(config.Settings(), types.SimpleNamespace(tier="cheap", vm_size=None, **base))
    _eq(s.vm_size, "Standard_F2s_v2", "--cheap maps to tiers.cheap")
    s2 = config.apply_overrides(config.Settings(), types.SimpleNamespace(tier="expensive", vm_size=None, **base))
    _eq(s2.vm_size, "Standard_F72s_v2", "--expensive maps to tiers.expensive")
    # explicit --vm-size beats a tier flag
    s3 = config.apply_overrides(config.Settings(), types.SimpleNamespace(tier="cheap", vm_size="Standard_X", **base))
    _eq(s3.vm_size, "Standard_X", "--vm-size overrides tier")


@test
def vm_create_identity_args():
    from azoffload import vm, azcli
    cap = {}
    orig = azcli.json_out
    azcli.json_out = lambda args: (cap.__setitem__("args", list(args)) or {"identity": {"principalId": "pid"}})
    try:
        s = config.Settings(resource_group="rg", region="r", account="a", vm_size="Standard_F2s_v2")
        names = naming.resource_names("mjoff-x")
        vm.create_vm(s, "mjoff-x", names, {"k": "v"}, "/tmp/ci", system_identity=True)
        a = cap["args"]
        _eq(a[a.index("--assign-identity") + 1], "[system]", "system-assigned uses [system]")
        s.managed_identity = "/subs/x/id"
        vm.create_vm(s, "mjoff-x", names, {"k": "v"}, "/tmp/ci", system_identity=False)
        a = cap["args"]
        _eq(a[a.index("--assign-identity") + 1], "/subs/x/id", "user-assigned uses the resource id")
    finally:
        azcli.json_out = orig


@test
def pricing_selection_and_fallback():
    items = [
        {"productName": "F72s v2", "meterName": "F72s v2", "skuName": "F72s v2", "retailPrice": 3.0},
        {"productName": "F72s v2 Spot", "meterName": "F72s v2 Spot", "skuName": "F72s v2 Spot", "retailPrice": 0.4},
        {"productName": "F72s v2 Low Priority", "meterName": "F72s v2 Low Priority", "retailPrice": 0.2},
        {"productName": "F72s v2 Windows", "meterName": "F72s v2 Windows", "retailPrice": 6.0},
    ]
    orig = pricing._fetch
    pricing._fetch = lambda flt: items
    try:
        p_spot, _ = pricing.get_vm_price("eastus", "Standard_F72s_v2", spot=True)
        _eq(p_spot, 0.4, "spot picks Spot meter, not Low Priority")
        p_od, _ = pricing.get_vm_price("eastus", "Standard_F72s_v2", spot=False)
        _eq(p_od, 3.0, "on-demand excludes Spot/Low/Windows")
        pricing._fetch = lambda flt: []
        raised = False
        try:
            pricing.get_vm_price("eastus", "x", spot=True)
        except Exception:
            raised = True
        assert raised, "empty result must raise (caller falls back to config price)"
    finally:
        pricing._fetch = orig


@test
def cloudinit_valid_and_ordered():
    s = config.load(os.path.join(PROJ, "config.yaml"))
    rid = naming.new_run_id()
    env = cloudinit.build_env(s, rid, naming.blob_prefix(rid), 3780, 600, 0)
    ci = cloudinit.render(env)
    doc = yaml.safe_load(ci)
    _eq(len(doc["write_files"]), 4)
    assert len(ci) < 65535, "customData under Azure 64KB limit"
    cmds = [str(c) for c in doc["runcmd"]]
    wd = next(i for i, c in enumerate(cmds) if "watchdog.sh" in c)
    apt = next(i for i, c in enumerate(cmds) if "apt-get install" in c)
    assert wd < apt, "watchdog must start before apt so max-lifetime is enforced even if apt hangs"
    # the embedded scripts are the real source files
    for wf in doc["write_files"]:
        if wf["path"].endswith("watchdog.sh"):
            decoded = base64.b64decode(wf["content"]).decode()
            assert "deallocate_self" in decoded and "169.254.169.254" in decoded
    assert "MJOFF_IDENTITY_CLIENT_ID" in env, "identity client id always present (empty for system)"
    # a user-assigned client id propagates verbatim into the sourced env file
    env2 = cloudinit.build_env(s, rid, naming.blob_prefix(rid), 3780, 600, 0, identity_client_id="abc-123")
    doc2 = yaml.safe_load(cloudinit.render(env2))
    envfile = next(base64.b64decode(w["content"]).decode()
                   for w in doc2["write_files"] if w["path"].endswith("/env"))
    assert "MJOFF_IDENTITY_CLIENT_ID=abc-123" in envfile


@test
def cloudinit_blocks_shell_injection():
    """A malicious config value must NOT execute when the env file is sourced."""
    sentinel = os.path.join(tempfile.mkdtemp(), "PWNED")
    s = config.load(os.path.join(PROJ, "config.yaml"))
    s.mujoco_gl = f"egl; touch {sentinel}"   # injection attempt via config
    rid = naming.new_run_id()
    env = cloudinit.build_env(s, rid, naming.blob_prefix(rid), 3780, 600, 0)
    doc = yaml.safe_load(cloudinit.render(env))
    envfile = os.path.join(tempfile.mkdtemp(), "env")
    for wf in doc["write_files"]:
        if wf["path"].endswith("/env"):
            with open(envfile, "wb") as f:
                f.write(base64.b64decode(wf["content"]))
    out = subprocess.run(
        ["bash", "-c", f'set -a; . "{envfile}"; set +a; printf "%s" "$MJOFF_MUJOCO_GL"'],
        capture_output=True, text=True)
    _eq(out.stdout, f"egl; touch {sentinel}", "value preserved literally")
    assert not os.path.exists(sentinel), "SECURITY: injected command executed!"


@test
def safe_extract_blocks_traversal():
    d = tempfile.mkdtemp()
    # benign archive extracts fine
    good = os.path.join(d, "good.tar.gz")
    payload = os.path.join(d, "f.txt")
    with open(payload, "w") as f:
        f.write("ok")
    with tarfile.open(good, "w:gz") as t:
        t.add(payload, arcname="sub/f.txt")
    dest = os.path.join(d, "out")
    with tarfile.open(good) as t:
        storage.safe_extractall(t, dest)
    assert os.path.exists(os.path.join(dest, "sub", "f.txt"))

    # malicious archive with ../ escape must raise
    evil = os.path.join(d, "evil.tar.gz")
    info = tarfile.TarInfo(name="../escape.txt")
    data = b"pwn"
    info.size = len(data)
    with tarfile.open(evil, "w:gz") as t:
        t.addfile(info, io.BytesIO(data))
    raised = False
    try:
        with tarfile.open(evil) as t:
            storage.safe_extractall(t, os.path.join(d, "out2"))
    except ValueError:
        raised = True
    assert raised, "SECURITY: path-traversal archive was not rejected"
    assert not os.path.exists(os.path.join(d, "escape.txt"))


def _settings_for_limits(**over):
    s = config.Settings()
    s.poll_interval_sec = 0
    s.stuck_timeout_min = 1
    s.boot_timeout_min = 1
    s.max_budget_usd = 10
    for k, v in over.items():
        setattr(s, k, v)
    return s


class _FakeStore:
    def __init__(self, prog):
        self._p = prog

    def get_json(self, name):
        return self._p


def _run_monitor(store, s, max_wall_sec, hourly, start_ts, preset_stop=False):
    lim = monitor.Limits(s, max_wall_sec)
    stop = threading.Event()
    if preset_stop:
        stop.set()
    return monitor.monitor(store, lim, hourly, start_ts, lambda *a: None, stop)


@test
def monitor_state_machine_all_paths():
    now = time.time()
    fresh = monitor._utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # success
    o = _run_monitor(_FakeStore({"state": "completed", "total": 5, "completed": 5,
                                 "updated_at": fresh}), _settings_for_limits(), 99999, 1.0, now)
    _eq(o.status, "success")

    # failed
    o = _run_monitor(_FakeStore({"state": "failed", "recent_errors": ["boom"],
                                 "updated_at": fresh}), _settings_for_limits(), 99999, 1.0, now)
    _eq(o.status, "failed")

    # budget (elapsed pushed 1h into past, hourly 100 -> cost 100 >= budget 10)
    o = _run_monitor(_FakeStore({"state": "running", "updated_at": fresh}),
                     _settings_for_limits(max_budget_usd=10), 99999, 100.0, now - 3600)
    _eq(o.status, "budget")

    # wallclock (elapsed beyond cap, tiny hourly so budget not hit first)
    o = _run_monitor(_FakeStore({"state": "running", "updated_at": fresh}),
                     _settings_for_limits(max_budget_usd=1e9), 60, 0.001, now - 600)
    _eq(o.status, "wallclock")

    # stuck (old heartbeat, within wall/budget)
    old = (monitor._utcnow() - __import__("datetime").timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    o = _run_monitor(_FakeStore({"state": "running", "updated_at": old}),
                     _settings_for_limits(max_budget_usd=1e9), 1e9, 0.001, now)
    _eq(o.status, "stuck")

    # boot_timeout (no progress file, elapsed beyond boot)
    o = _run_monitor(_FakeStore(None), _settings_for_limits(max_budget_usd=1e9), 1e9, 0.001, now - 600)
    _eq(o.status, "boot_timeout")

    # aborted (stop flag preset)
    o = _run_monitor(_FakeStore({"state": "running", "updated_at": fresh}),
                     _settings_for_limits(), 1e9, 0.001, now, preset_stop=True)
    _eq(o.status, "aborted")


@test
def monitor_eta_formatting():
    prog = {"state": "running", "total": 72, "completed": 18, "failed": 1,
            "throughput_per_min": 36.0, "eta_min": 1.5, "updated_at": "2026-06-28T00:00:00Z"}
    line = monitor.format_status(prog, 95.0, 0.63, 3.06)
    assert "18/72" in line and "25.0%" in line and "ETA 1.5m" in line, line
    assert "waiting" in monitor.format_status(None, 30.0, 0.05, 3.06)


@test
def storage_store_roundtrip_and_purge():
    rid = naming.new_run_id()
    svc, mode = storage.make_service("acct", "https://acct.blob.core.windows.net",
                                     "mjoff", "rbac")
    _eq(mode, "rbac(az-login)")
    st = storage.Store(svc, "mjoff", naming.blob_prefix(rid))
    st.ensure_container()
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "p.json")
    with open(fp, "w") as f:
        f.write('{"state":"completed","completed":3}')
    st.upload_file("progress/progress.json", fp)
    got = st.get_json("progress/progress.json")
    _eq(got["completed"], 3)
    assert st.download("progress/progress.json", os.path.join(d, "dl.json"))
    assert not st.download("missing/blob.bin", os.path.join(d, "no.bin"))
    n = st.purge_prefix()
    assert n >= 1
    assert st.get_json("progress/progress.json") is None


@test
def teardown_idempotent_helpers():
    assert teardown._is_gone(0, "")
    assert teardown._is_gone(1, "ResourceNotFound: ...")
    assert teardown._is_gone(1, "The Resource was not found")
    assert not teardown._is_gone(1, "some real error")

    from azoffload import azcli
    calls = []
    o_try, o_safe = azcli.try_run, azcli.safe_json
    azcli.try_run = lambda args: (calls.append(args) or (0, ""))
    azcli.safe_json = lambda args, default=None: []
    try:
        s = config.Settings(resource_group="rg")
        res = teardown.teardown_compute(s, "mjoff-x", log=lambda *a: None)
    finally:
        azcli.try_run, azcli.safe_json = o_try, o_safe
    verbs = [" ".join(c[:2]) for c in calls]
    for expect in ("vm delete", "network public-ip", "network nsg", "disk delete"):
        assert any(expect in v for v in verbs), f"teardown missing {expect}: {verbs}"
    assert all(ok for _, ok, _ in res), "all teardown steps idempotently ok"


@test
def no_shell_true_in_source():
    bad = []
    for root, _, files in os.walk(os.path.join(PROJ, "azoffload")):
        for fn in files:
            if fn.endswith(".py"):
                txt = open(os.path.join(root, fn)).read()
                if "shell=True" in txt:
                    bad.append(os.path.join(root, fn))
    _eq(bad, [], "no subprocess shell=True anywhere")
    # watchdog must never echo/log the bearer token
    wd = open(os.path.join(PROJ, "azoffload", "remote", "watchdog.sh")).read()
    for line in wd.splitlines():
        if "LOG" in line or "echo" in line:
            assert "TOKEN" not in line, "SECURITY: token must not be logged"


# ----------------------------------------------------------------------------
# End-to-end: run the REAL runner.py over the real MuJoCo job via fake Azure.
def integration_runner():
    scratch = os.environ.get("MJOFF_TEST_JOBENV")
    if not scratch or not os.path.exists(os.path.join(scratch, "bin", "python")):
        print("  SKIP integration: set MJOFF_TEST_JOBENV to a venv with numpy+mujoco")
        return "skip"

    work = tempfile.mkdtemp(prefix="mjoff-e2e-")
    blobroot = os.path.join(work, "blob")
    vmroot = os.path.join(work, "vm")
    os.makedirs(vmroot)
    os.environ_e2e = blobroot

    # trimmed bundle: 8 scenarios, short rollouts, no render deps needed
    bundle = os.path.join(work, "bundle")
    os.makedirs(bundle)
    shutil.copy(os.path.join(PROJ, "sample_bundle", "job.py"), bundle)
    import json as _json
    scn = [{"id": i, "gravity": -9.81, "damping": 0.1, "ctrl_amp": 0.4,
            "ctrl_freq": 1.0, "steps": 300, "seed": i} for i in range(8)]
    _json.dump({"scenarios": scn}, open(os.path.join(bundle, "scenarios.json"), "w"))
    open(os.path.join(bundle, "requirements.txt"), "w").write("numpy\nmujoco\n")

    rid = naming.new_run_id()
    prefix = naming.blob_prefix(rid)
    # stage bundle into the fake blob store
    inp = os.path.join(blobroot, "mjoff", prefix, "input")
    os.makedirs(inp)
    storage.make_bundle(bundle, os.path.join(inp, "bundle.tar.gz"))
    shutil.copy(os.path.join(PROJ, "azoffload", "remote", "worker.py"),
                os.path.join(vmroot, "worker.py"))

    env = dict(os.environ)
    env["PYTHONPATH"] = FAKE   # runner imports the fake azure
    env["BLOB_ROOT"] = blobroot
    env.update({
        "MJOFF_ACCOUNT_URL": "https://acct.blob.core.windows.net",
        "MJOFF_CONTAINER": "mjoff", "MJOFF_RUN_ID": rid, "MJOFF_PREFIX": prefix,
        "MJOFF_NPROC": "4", "MJOFF_MUJOCO_GL": "disable",
        "MJOFF_JOB_MODULE": "job", "MJOFF_ENTRY": "run_scenario",
        "MJOFF_SCENARIOS": "scenarios.json",
        "MJOFF_BASE": vmroot, "MJOFF_JOBENV": scratch,
        "MJOFF_WORKER": os.path.join(vmroot, "worker.py"),
        "MJOFF_REUSE_JOBENV": "1", "MJOFF_HEARTBEAT_INTERVAL": "3",
        "MJOFF_HEARTBEAT_FILE": os.path.join(vmroot, "heartbeat"),
        "MJOFF_RUNNER_STARTED": os.path.join(vmroot, "RUNNER_STARTED"),
        "MJOFF_COMPLETE": os.path.join(vmroot, "COMPLETE"),
    })
    t0 = time.time()
    p = subprocess.run([sys.executable, os.path.join(PROJ, "azoffload", "remote", "runner.py")],
                       env=env, capture_output=True, text=True, timeout=600)
    dt = time.time() - t0
    if p.returncode != 0:
        print("  runner stdout:\n" + p.stdout[-2000:])
        print("  runner stderr:\n" + p.stderr[-2000:])
        raise AssertionError(f"runner exited {p.returncode}")

    import json as _j
    prog = _j.load(open(os.path.join(blobroot, "mjoff", prefix, "progress", "progress.json")))
    _eq(prog["state"], "completed", "final state")
    _eq(prog["completed"], 8, "all scenarios completed")
    _eq(prog["failed"], 0, "no failures")
    assert prog["throughput_per_min"] > 0, "throughput measured"
    assert os.path.exists(os.path.join(blobroot, "mjoff", prefix, "output", "results.tar.gz"))
    assert os.path.exists(os.path.join(vmroot, "COMPLETE")), "COMPLETE marker (triggers watchdog deallocate)"

    # results archive is rooted at run-id and contains per-scenario outputs
    base = os.path.join(work, "dl")
    storage.extract_results(os.path.join(blobroot, "mjoff", prefix, "output", "results.tar.gz"), base)
    sdir = os.path.join(base, rid)
    got = sorted(os.listdir(sdir))
    _eq(len(got), 8, "8 scenario dirs in results")
    assert os.path.exists(os.path.join(sdir, "scenario_00000", "_result.json"))
    print(f"  e2e: 8 scenarios, parallel, real MuJoCo, {dt:.1f}s, results verified")
    return "pass"


def main():
    print("=" * 64)
    print("mujoco-azure-offload  test suite")
    print("=" * 64)
    passed = failed = 0
    for fn in RESULTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print("-" * 64)
    print("integration (real runner.py + MuJoCo via fake Azure):")
    try:
        r = integration_runner()
        if r == "pass":
            print("  PASS  integration_runner")
            passed += 1
        else:
            print("  SKIP  integration_runner")
    except Exception as e:
        print(f"  FAIL  integration_runner: {e}")
        failed += 1
    print("=" * 64)
    print(f"RESULT: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
