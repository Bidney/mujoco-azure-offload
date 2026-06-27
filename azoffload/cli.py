"""mujoco-azure-offload command-line entrypoint.

Subcommands:
  run            package -> upload -> provision spot VM -> monitor -> teardown -> download
  teardown-only  find & remove everything this tool created (by run-id tag + blob prefix)
  status         re-attach to a running job's progress (e.g. after the laptop slept)
"""
import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time

from . import azcli, cloudinit, config, monitor, naming, pricing, storage, teardown, vm


# ----------------------------- helpers -----------------------------

def _eprint(*a):
    print(*a, file=sys.stderr)


def _resolve_subscription(s):
    if s.subscription_id:
        azcli.run(["account", "set", "--subscription", s.subscription_id])
    acct = azcli.json_out(["account", "show"])
    if not acct:
        raise SystemExit("Not logged in. Run `az login` first.")
    return acct.get("id"), acct.get("name")


def _get_price(s):
    if s.spot:
        try:
            p, src = pricing.get_vm_price(s.region, s.vm_size, spot=True)
            return p, src
        except Exception as e:
            _eprint(f"  ! Retail Prices API failed ({e}); using fallback spot price.")
            return s.fallback_spot_hourly_usd, "config-fallback"
    try:
        p, src = pricing.get_vm_price(s.region, s.vm_size, spot=False)
        return p, src
    except Exception as e:
        _eprint(f"  ! Retail Prices API failed ({e}); using fallback on-demand price.")
        return s.fallback_hourly_usd, "config-fallback"


def _count_scenarios(bundle_dir, scenarios_file):
    path = os.path.join(bundle_dir, scenarios_file)
    with open(path) as f:
        sc = json.load(f)
    if isinstance(sc, dict) and "scenarios" in sc:
        sc = sc["scenarios"]
    return len(sc)


def _validate_bundle(s, bundle_dir):
    problems = []
    if not os.path.isdir(bundle_dir):
        return [f"bundle dir not found: {bundle_dir}"]
    if not os.path.exists(os.path.join(bundle_dir, s.job_module + ".py")):
        problems.append(f"bundle missing {s.job_module}.py (must expose {s.entry_function}(scenario, workdir))")
    if not os.path.exists(os.path.join(bundle_dir, s.scenarios_file)):
        problems.append(f"bundle missing {s.scenarios_file}")
    if not os.path.exists(os.path.join(bundle_dir, "requirements.txt")):
        problems.append("bundle missing requirements.txt")
    return problems


def _itemized_estimate(s, hourly_vm, eff_minutes):
    hours = eff_minutes / 60.0
    vm = hourly_vm * hours
    disk = s.disk_hourly_usd * hours
    ip = s.ip_hourly_usd * hours
    storage_cost = s.storage_flat_usd
    total = vm + disk + ip + storage_cost
    lines = [
        ("VM compute  (%s %s)" % (s.vm_size, "spot" if s.spot else "DEDICATED"),
         hourly_vm, vm),
        ("OS disk     (%dGB)" % s.os_disk_size_gb, s.disk_hourly_usd, disk),
        ("Public IP   (standard)", s.ip_hourly_usd, ip),
        ("Blob storage + egress (<100MB)", None, storage_cost),
    ]
    return lines, total


def _hourly_total(s, hourly_vm):
    return hourly_vm + s.disk_hourly_usd + s.ip_hourly_usd


def _confirm(prompt):
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ----------------------------- run -----------------------------

def cmd_run(s, args):
    bundle_dir = os.path.abspath(args.bundle)
    sub_id, sub_name = _resolve_subscription(s)

    problems = config.validate_for_run(s) + _validate_bundle(s, bundle_dir)
    if problems:
        _eprint("Configuration problems:")
        for p in problems:
            _eprint("  - " + p)
        raise SystemExit(2)

    run_id = naming.new_run_id()
    names = naming.resource_names(run_id)
    tags = naming.tags(run_id, owner=s.owner)
    prefix = naming.blob_prefix(run_id)
    nproc = s.nproc  # 0 => runner uses all cores
    total = _count_scenarios(bundle_dir, s.scenarios_file)

    # Identity: blank managed_identity -> system-assigned + auto-roles (nothing in config).
    system_identity = not s.managed_identity
    identity_client_id = "" if system_identity else vm.get_identity_client_id(s.managed_identity)

    hourly_vm, price_src = _get_price(s)
    hourly_total = _hourly_total(s, hourly_vm)

    # Effective wall clock = the tighter of the user's wall-clock cap and what the
    # budget can afford. This becomes the CLOUD-SIDE watchdog max-lifetime, so even
    # if the laptop dies the spend is bounded by --max-budget.
    affordable_min = (s.max_budget_usd / hourly_total) * 60.0 if hourly_total > 0 else s.max_wall_clock_min
    eff_minutes = min(s.max_wall_clock_min, affordable_min)
    max_wall_sec = eff_minutes * 60.0
    watchdog_lifetime_sec = max_wall_sec + 180  # small buffer for final upload
    stuck_sec = s.stuck_timeout_min * 60.0

    # ---- print plan + itemized estimate ----
    print("=" * 70)
    print(f"mujoco-azure-offload  run-id={run_id}")
    print(f"  subscription : {sub_name} ({sub_id})")
    print(f"  resource grp : {s.resource_group}   region: {s.region}")
    print(f"  storage      : {s.account}/{s.container}   prefix: {prefix}")
    tier_note = f" (--{args.tier})" if getattr(args, "tier", None) else ""
    print(f"  VM           : {s.vm_size}{tier_note}  priority={'SPOT' if s.spot else 'DEDICATED'}")
    id_desc = ("system-assigned + auto-roles (deallocate on this VM, blob on this container)"
               if system_identity else f"user-assigned ({s.managed_identity.split('/')[-1]})")
    print(f"  VM identity  : {id_desc}")
    print(f"  bundle       : {bundle_dir}  ({total} scenarios, nproc={nproc or 'all cores'})")
    print(f"  price source : {price_src}  -> ${hourly_vm:.4f}/hr (vm) ${hourly_total:.4f}/hr (all-in)")
    print("-" * 70)
    lines, total_cost = _itemized_estimate(s, hourly_vm, eff_minutes)
    print(f"  Itemized cost ceiling (if it runs the full {eff_minutes:.0f} min cap):")
    for label, rate, cost in lines:
        rate_s = f"${rate:.4f}/hr" if rate is not None else "flat"
        print(f"    {label:<34} {rate_s:>14}   ${cost:6.2f}")
    print(f"    {'TOTAL WORST-CASE':<34} {'':>14}   ${total_cost:6.2f}")
    print("-" * 70)
    print(f"  Hard caps     : budget ${s.max_budget_usd:.2f} | wall-clock {s.max_wall_clock_min:.0f} min")
    print(f"  Cloud cap     : VM self-deallocates after {eff_minutes:.0f} min "
          f"(= min(wall-clock, budget/price)) — enforced on the VM, laptop-independent")
    print(f"  Stuck cap     : abort if no heartbeat for {s.stuck_timeout_min:.0f} min")
    print("=" * 70)

    if args.dry_run:
        env = cloudinit.build_env(s, run_id, prefix, watchdog_lifetime_sec, stuck_sec, nproc,
                              identity_client_id=identity_client_id)
        ci = cloudinit.render(env)
        print(f"[dry-run] cloud-init rendered ({len(ci)} bytes). No resources created.")
        print(f"[dry-run] would create: vm={names['vm']} ip={names['ip']} "
              f"nsg={names['nsg']} disk={names['disk']}")
        return 0

    if not args.yes and not _confirm("Proceed and incur the cost above? [y/N] "):
        print("Aborted by user. Nothing was created.")
        return 1

    # ---- storage ----
    svc, mode = storage.make_service(s.account, s.account_url, s.container, s.storage_auth)
    print(f"  blob auth     : {mode}")
    store = storage.Store(svc, s.container, prefix)
    store.ensure_container()

    tmp = tempfile.mkdtemp(prefix="mjoff-")
    bundle_tar = os.path.join(tmp, "bundle.tar.gz")
    storage.make_bundle(bundle_dir, bundle_tar)
    print("  uploading bundle…")
    store.upload_file("input/bundle.tar.gz", bundle_tar)

    env = cloudinit.build_env(s, run_id, prefix, watchdog_lifetime_sec, stuck_sec, nproc,
                              identity_client_id=identity_client_id)
    ci_path = os.path.join(tmp, "cloud-init.yaml")
    with open(ci_path, "w") as f:
        f.write(cloudinit.render(env))

    # ---- teardown plumbing (runs on EVERY exit path, idempotent) ----
    torn = {"done": False}
    started_ts = {"t": None}

    def do_teardown(reason):
        if torn["done"]:
            return
        torn["done"] = True
        print(f"\nTearing down (reason: {reason})…")
        teardown.teardown_compute(s, run_id)

    stop_flag = threading.Event()

    def _sig(signum, _frame):
        _eprint(f"\nReceived signal {signum}; aborting and tearing down…")
        stop_flag.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    outcome = None
    try:
        print("  provisioning VM (this takes ~1-2 min)…")
        vm.create_vm(s, run_id, names, tags, ci_path, system_identity=system_identity)
        started_ts["t"] = time.time()  # billing clock starts ~ now
        try:
            vm.tag_aux_resources(s, names, tags)
        except Exception:
            pass
        if system_identity:
            try:
                print("  granting the VM identity tightly-scoped roles (deallocate + blob)…")
                vm.assign_identity_roles(s, sub_id, names)
            except PermissionError as e:
                _eprint(f"\n  ! {e}")
                _eprint("  ! Without the deallocate role the VM can't self-destruct — aborting + tearing down.")
                _eprint("  ! Fix: run as Owner/User Access Administrator, or pre-create a user-assigned")
                _eprint("  !      identity (2 roles) and set compute.managed_identity. See README.")
                raise
        print(f"  VM {names['vm']} created. Monitoring (poll {s.poll_interval_sec}s)…\n")

        lim = monitor.Limits(s, max_wall_sec)

        def on_status(prog, elapsed, cost):
            sys.stdout.write("\r" + monitor.format_status(prog, elapsed, cost, hourly_total)
                             + " " * 6)
            sys.stdout.flush()

        outcome = monitor.monitor(store, lim, hourly_total, started_ts["t"], on_status, stop_flag)
        print()  # newline after the \r status line
    except KeyboardInterrupt:
        outcome = monitor.Outcome("aborted", "KeyboardInterrupt", 0,
                                  time.time() - (started_ts["t"] or time.time()), {})
    except Exception as e:
        _eprint(f"\nError during run: {e}")
        outcome = monitor.Outcome("error", str(e), 0,
                                  time.time() - (started_ts["t"] or time.time()), {})
    finally:
        do_teardown(outcome.status if outcome else "unknown")

    # ---- results ----
    results_path = None
    if outcome and outcome.status == "success":
        print("  downloading results…")
        local_tar = os.path.join(tmp, "results.tar.gz")
        if store.download("output/results.tar.gz", local_tar):
            base = os.path.abspath(args.results_dir)
            storage.extract_results(local_tar, base)        # archive root == run_id
            results_path = os.path.join(base, run_id)
            # results safely local -> remove this run's blobs unless asked to keep
            if not args.keep_blobs:
                store.purge_prefix()
        else:
            _eprint("  ! results archive not found in blob (job may have aborted early).")
    else:
        # keep blobs on non-success so partial output/logs can be inspected
        for blob, dest in (("logs/runner.boot.log", "runner.boot.log"),
                           ("logs/watchdog.log", "watchdog.log")):
            store.download(blob, os.path.join(tmp, dest))

    # ---- one-line summary ----
    wall = (outcome.elapsed_sec / 60.0) if outcome else 0.0
    approx_cost = outcome.cost if outcome else 0.0
    print("=" * 70)
    status = outcome.status if outcome else "unknown"
    print(f"SUMMARY  run-id={run_id}  status={status}  wall={wall:.1f}m  "
          f"approx_cost=${approx_cost:.2f}")
    if outcome and outcome.detail:
        print(f"         detail: {outcome.detail}")
    if results_path:
        print(f"         results: {results_path}")
    elif status != "success":
        print(f"         partial logs/blobs kept under blob prefix {prefix} "
              f"(clean later: offload teardown-only --run-id {run_id} --purge-blobs)")
    print("=" * 70)

    return 0 if status == "success" else 1


# ----------------------------- teardown-only -----------------------------

def cmd_teardown_only(s, args):
    _resolve_subscription(s)
    if args.all:
        ids = teardown.find_runs(s)
        if not ids:
            print("No tool-created resources found.")
            return 0
        print("Found run-ids with live resources:")
        for r in ids:
            print("  - " + r)
        if not args.yes and not _confirm("Delete ALL of the above? [y/N] "):
            return 1
        targets = ids
    elif args.run_id:
        targets = [args.run_id]
    else:
        raise SystemExit("Provide --run-id <id> or --all")

    for run_id in targets:
        print(f"Tearing down {run_id}…")
        teardown.teardown_compute(s, run_id)
        if args.purge_blobs:
            try:
                svc, _ = storage.make_service(s.account, s.account_url, s.container, s.storage_auth)
                n = storage.Store(svc, s.container, naming.blob_prefix(run_id)).purge_prefix()
                print(f"  purged {n} blob(s) under {naming.blob_prefix(run_id)}")
            except Exception as e:
                _eprint(f"  ! blob purge failed: {e}")
    print("Done. Verify with: az resource list --tag "
          f"{naming.TOOL_TAG_KEY}={naming.TOOL_TAG} -o table")
    return 0


# ----------------------------- status -----------------------------

def cmd_status(s, args):
    _resolve_subscription(s)
    svc, _ = storage.make_service(s.account, s.account_url, s.container, s.storage_auth)
    store = storage.Store(svc, s.container, naming.blob_prefix(args.run_id))
    prog = store.get_json("progress/progress.json")
    if not prog:
        print("No progress file found (job not started, already cleaned, or wrong run-id).")
        return 1
    print(json.dumps(prog, indent=2))
    return 0


# ----------------------------- argparse -----------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="offload",
                                description="Offload a MuJoCo parameter sweep to a single Azure spot VM.")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")

    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="package, provision, run, monitor, teardown, download")
    r.add_argument("--bundle", default="sample_bundle", help="input bundle folder")
    r.add_argument("--results-dir", default="results", help="where to extract results locally")
    r.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
    r.add_argument("--dry-run", action="store_true", help="show plan + estimate, create nothing")
    r.add_argument("--force-dedicated", action="store_true", help="use a dedicated (non-spot) VM")
    r.add_argument("--keep-blobs", action="store_true", help="don't purge blobs after download")
    # config overrides
    r.add_argument("--subscription")
    r.add_argument("--resource-group")
    r.add_argument("--region")
    r.add_argument("--account")
    r.add_argument("--container")
    r.add_argument("--vm-size")
    tier_grp = r.add_mutually_exclusive_group()
    tier_grp.add_argument("--cheap", action="store_const", const="cheap", dest="tier",
                          help="use config.tiers.cheap (default Standard_F2s_v2)")
    tier_grp.add_argument("--moderate", action="store_const", const="moderate", dest="tier",
                          help="use config.tiers.moderate (default Standard_F16s_v2)")
    tier_grp.add_argument("--expensive", action="store_const", const="expensive", dest="tier",
                          help="use config.tiers.expensive (default Standard_F72s_v2)")
    r.add_argument("--nproc", type=int)
    r.add_argument("--managed-identity")
    r.add_argument("--max-budget", type=float, help="USD")
    r.add_argument("--max-wall-clock", type=float, help="minutes")
    r.add_argument("--stuck-timeout", type=float, help="minutes")

    t = sub.add_parser("teardown-only", help="remove resources this tool created")
    t.add_argument("--run-id")
    t.add_argument("--all", action="store_true", help="every tool-created run")
    t.add_argument("--purge-blobs", action="store_true", help="also delete the run's blobs")
    t.add_argument("--yes", action="store_true")
    t.add_argument("--account")
    t.add_argument("--resource-group")
    t.add_argument("--subscription")

    st = sub.add_parser("status", help="print a running job's progress JSON")
    st.add_argument("--run-id", required=True)
    st.add_argument("--account")
    st.add_argument("--resource-group")
    st.add_argument("--subscription")

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    s = config.load(args.config)
    s = config.apply_overrides(s, args)
    if args.cmd == "run":
        sys.exit(cmd_run(s, args))
    elif args.cmd == "teardown-only":
        sys.exit(cmd_teardown_only(s, args))
    elif args.cmd == "status":
        sys.exit(cmd_status(s, args))


if __name__ == "__main__":
    main()
