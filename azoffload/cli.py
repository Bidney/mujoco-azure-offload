"""mujoco-azure-offload command-line entrypoint.

Subcommands:
  run            package -> upload -> provision spot VM -> monitor -> teardown -> download
  teardown-only  find & remove everything this tool created (by run-id tag + blob prefix)
  status         re-attach to a running job's progress (e.g. after the laptop slept)
"""
import argparse
import json
import os
import re
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


def _vcpus_from_size(size):
    """Best-effort vCPU count from an Azure size name (e.g. Standard_F16s_v2 -> 16)."""
    m = re.search(r"_[A-Za-z]+(\d+)", size or "")
    return int(m.group(1)) if m else 0


def _fallback_price(s):
    """Estimate $/hr when the pricing API is down, scaled by vCPU count so it's sane
    for any size (not the hardcoded F72 price)."""
    vcpus = _vcpus_from_size(s.vm_size)
    if vcpus:
        rate = s.fallback_per_vcpu_hour_spot if s.spot else s.fallback_per_vcpu_hour
        return rate * vcpus
    return s.fallback_spot_hourly_usd if s.spot else s.fallback_hourly_usd


def _get_price(s):
    try:
        return pricing.get_vm_price(s.region, s.vm_size, spot=s.spot)
    except Exception as e:
        est = _fallback_price(s)
        n = _vcpus_from_size(s.vm_size)
        _eprint(f"  ! Retail Prices API unavailable ({e}); estimating "
                f"{'spot' if s.spot else 'on-demand'} from {n or '?'} vCPU -> ${est:.4f}/hr.")
        return est, "config-fallback(per-vCPU)"


def _sku_unavailable_hint(s):
    if s.spot:
        return (f"{s.vm_size} spot capacity is unavailable in {s.region} right now "
                f"(spot is limited, especially on Visual Studio subscriptions). "
                f"Try --force-dedicated, a different --region, or another --vm-size/tier.")
    return f"{s.vm_size} is unavailable in {s.region}. Try a different --region or --vm-size."


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


def _prompt(label, default, flag_hint, yes):
    """Resolve a target value: interactive prompt (default pre-filled), or with
    --yes fall back to the config default / error. Nothing is hardcoded."""
    if yes:
        if default:
            return default
        raise SystemExit(f"--yes set but {label} is unset; pass {flag_hint} or set it in config.")
    try:
        suffix = f" [{default}]" if default else ""
        entered = input(f"  {label}{suffix}: ").strip()
    except EOFError:
        raise SystemExit(f"No interactive terminal to prompt for {label}; pass {flag_hint}.")
    val = entered or default
    if not val:
        raise SystemExit(f"{label} is required.")
    return val


def resolve_and_confirm_subscription(s, args):
    """Make the user explicitly choose the subscription before anything is created,
    so resources can never land in an unintended subscription."""
    explicit = args.subscription or s.subscription_id
    if not explicit and getattr(args, "yes", False):
        raise SystemExit("Refusing to run with --yes and no explicit subscription. "
                         "Pass --subscription <id> so resources can't land in the wrong place.")
    active = azcli.json_out(["account", "show"])
    if not active:
        raise SystemExit("Not logged in. Run `az login` first.")
    chosen = explicit or _prompt("Subscription ID", active.get("id"), "--subscription", yes=False)
    if chosen != active.get("id"):
        azcli.run(["account", "set", "--subscription", chosen])
    acct = azcli.json_out(["account", "show"])
    # `az account set` accepts a subscription name as well as an id.
    if not acct or chosen not in (acct.get("id"), acct.get("name")):
        raise SystemExit(f"Could not switch to subscription {chosen!r}. Check the id and `az login`.")
    return acct.get("id"), acct.get("name")


def resolve_machine(s, args):
    """Pick the VM size: a tier flag / --vm-size wins; otherwise prompt (tier name
    or an explicit size). With --yes, fall back to the config default."""
    if args.vm_size or getattr(args, "tier", None):
        return s.vm_size  # already applied by config.apply_overrides
    if getattr(args, "yes", False):
        return s.vm_size
    print("  Machine (pick a tier name or type a size like Standard_F8s_v2):")
    for name in ("cheap", "moderate", "expensive"):
        print(f"      {name:<10} {s.tiers.get(name)}")
    choice = _prompt("tier or size", "cheap", "--cheap/--moderate/--expensive/--vm-size", yes=False)
    return s.tiers.get(choice, choice)   # tier name -> size, else treat input as a literal size


# ----------------------------- run -----------------------------

def cmd_run(s, args):
    bundle_dir = os.path.abspath(args.bundle)

    print("Choose the target (nothing is created until you confirm the cost):")
    sub_id, sub_name = resolve_and_confirm_subscription(s, args)
    if args.resource_group is None:
        s.resource_group = _prompt("Resource group", s.resource_group, "--resource-group", yes=args.yes)
    if args.account is None:
        s.account = _prompt("Storage account", s.account, "--account", yes=args.yes)
    s.vm_size = resolve_machine(s, args)

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
    if not system_identity and not identity_client_id:
        # An empty client id would make the watchdog request an IMDS token as if the VM
        # had a system-assigned identity (it won't), so it could never self-deallocate.
        raise SystemExit(
            f"could not resolve the clientId of managed identity {s.managed_identity!r} "
            "(az identity show failed — check the resource id and your az login). "
            "Refusing to launch: without it the VM's watchdog cannot self-deallocate.")

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
    store = storage.Store(svc, s.container, prefix, account=s.account, account_url=s.account_url,
                          can_fallback_to_key=(s.storage_auth == "auto"))
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
        elapsed = time.time() - (started_ts["t"] or time.time())
        outcome = monitor.Outcome("aborted", "KeyboardInterrupt",
                                  hourly_total * elapsed / 3600.0, elapsed, {})
    except Exception as e:
        msg = str(e)
        if "SkuNotAvailable" in msg or "Capacity Restrictions" in msg:
            detail = _sku_unavailable_hint(s)
            _eprint("\n  ! " + detail)
        else:
            detail = msg
            _eprint(f"\nError during run: {e}")
        elapsed = time.time() - (started_ts["t"] or time.time())
        outcome = monitor.Outcome("error", detail,
                                  hourly_total * elapsed / 3600.0, elapsed, {})
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
        fetched = []
        for blob, dest in (("logs/runner.boot.log", "runner.boot.log"),
                           ("logs/watchdog.log", "watchdog.log")):
            path = os.path.join(tmp, dest)
            if store.download(blob, path):
                fetched.append(path)
        if fetched:
            print("  VM logs saved to:")
            for path in fetched:
                print("    " + path)

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
    if not s.resource_group:
        # With an empty -g every `az ... delete` fails with "resource group '' could not
        # be found", which the idempotent teardown treats as already-gone — the sweep
        # would report success while deleting nothing.
        raise SystemExit("azure.resource_group is required (pass --resource-group or set it in config).")
    if args.purge_blobs and not s.account:
        raise SystemExit("--purge-blobs needs storage.account (pass --account or set it in config).")
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
                n = storage.Store(svc, s.container, naming.blob_prefix(run_id),
                                  account=s.account, account_url=s.account_url,
                                  can_fallback_to_key=(s.storage_auth == "auto")).purge_prefix()
                print(f"  purged {n} blob(s) under {naming.blob_prefix(run_id)}")
            except Exception as e:
                _eprint(f"  ! blob purge failed: {e}")
    print("Done. Verify with: az resource list --tag "
          f"{naming.TOOL_TAG_KEY}={naming.TOOL_TAG} -o table")
    return 0


# ----------------------------- status -----------------------------

def cmd_status(s, args):
    _resolve_subscription(s)
    if not s.account:
        raise SystemExit("storage.account is required (pass --account or set it in config).")
    svc, _ = storage.make_service(s.account, s.account_url, s.container, s.storage_auth)
    store = storage.Store(svc, s.container, naming.blob_prefix(args.run_id),
                          account=s.account, account_url=s.account_url,
                          can_fallback_to_key=(s.storage_auth == "auto"))
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
