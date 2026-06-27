# mujoco-azure-offload

[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.9%2B-blue.svg)

Offload a CPU-heavy, embarrassingly-parallel **MuJoCo parameter sweep** to a single
large Azure **spot** VM, monitor it with a real-throughput ETA, and **guarantee the
expensive compute never keeps billing** — even if your laptop dies.

> **Status:** the compute orchestration, blob I/O, abort/ETA state machine, cloud-init
> rendering and security hardening are covered by an offline test suite that runs the
> real on-VM code against a fake Azure SDK. The live Azure control plane (`az vm create`
> / teardown / in-guest deallocate) is validated by construction and `--dry-run`, not yet
> by a recorded real provision — do a small `--max-budget`-capped run first. See
> [Disclaimer](#disclaimer).

- **Compute:** one `Standard_F72s_v2` (72 vCPU) spot VM. The sweep fans out across all
  cores via per-scenario subprocesses. (Single VM chosen over Azure Batch for
  simplicity: no Batch account, one resource to reason about, and a self-contained
  cloud-side watchdog gives the same "self-destruct" guarantee.)
- **Auth:** reuses your interactive `az login` (`AzureCliCredential` locally,
  the VM's **managed identity** remotely). No secrets are ever prompted for or stored.
- **Cost-safety:** `--max-budget` + `--max-wall-clock`, an itemized pre-launch estimate,
  an **independent cloud-side self-destruct**, idempotent teardown on every exit path,
  and a `teardown-only` sweep.

---

## How the cloud-side self-destruct works (and why it survives your laptop dying)

The VM boots with a **user-assigned managed identity** and a cloud-init **watchdog**
(`azoffload/remote/watchdog.sh`) started *first thing at boot*. The watchdog loops and
**self-deallocates the VM via the Azure REST API** when any of these happen:

1. the job finishes (writes a `COMPLETE` marker) — stop billing immediately, don't wait
   for the controller;
2. an **absolute max-lifetime** is exceeded;
3. the job is **stuck** (no heartbeat update for N minutes).

It gets an AAD token and the VM's own resource id from **IMDS**
(`169.254.169.254`, link-local, always available) and POSTs `…/deallocate`. No `az`
install, no embedded secret, and **no dependency on the controller process**. If your
laptop loses network or dies, the VM still tears its own compute down.

The max-lifetime is set to **`min(--max-wall-clock, --max-budget ÷ hourly-price)`**, so
even in the laptop-died case your spend is bounded by your budget.

> **Deallocate vs delete.** Deallocating stops the *expensive* F72-class compute billing
> instantly and needs only the VM's own identity. The residual cheap OS disk + public IP
> (cents/hour) are removed by the controller's teardown on normal exit, or later by
> `offload teardown-only --run-id <id>` (works purely from the run-id tag/name, no local
> state needed). So nothing *expensive* ever lingers, and a one-command sweep removes the
> rest. (If the REST call ever fails after retries, the watchdog falls back to a guest
> power-off and logs that the controller must finish deletion.)

---

## One-time setup

Prerequisites: the **Azure CLI** (`az`) and Python 3.9+.

```bash
# 0) sign in interactively (the tool reuses THIS credential; it never asks for secrets)
az login
az account set --subscription "<YOUR_SUBSCRIPTION_ID>"

# pick names/region
RG=mjoff-rg
LOC=eastus
SA=mjoffstorage$RANDOM        # storage account names are global + lowercase
ID=mjoff-watchdog-identity

# 1) resource group
az group create -n $RG -l $LOC

# 2) storage account (the 'mjoff' container is auto-created on first run)
az storage account create -n $SA -g $RG -l $LOC --sku Standard_LRS

# 3) user-assigned managed identity the VM will run as
az identity create -n $ID -g $RG -l $LOC
ID_RESID=$(az identity show -n $ID -g $RG --query id -o tsv)
ID_PRINCIPAL=$(az identity show -n $ID -g $RG --query principalId -o tsv)
SUB=$(az account show --query id -o tsv)

# 4) roles for that identity:
#    - Virtual Machine Contributor (scoped to the RG) -> lets the watchdog deallocate the VM
#    - Storage Blob Data Contributor -> lets the job read the bundle / write results
az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Virtual Machine Contributor" --scope "/subscriptions/$SUB/resourceGroups/$RG"
az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$SA"

# 5) (recommended) let YOUR user write blobs over RBAC too, so the controller needs no key:
ME=$(az ad signed-in-user show --query id -o tsv)
az role assignment create --assignee-object-id $ME --assignee-principal-type User \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$SA"

echo "managed_identity: $ID_RESID"   # paste into config.yaml -> compute.managed_identity
```

> If you skip step 5, set `storage.auth_mode: auto` (the default) and the tool falls back
> to the account **key** (fetched at runtime via `az`, kept in memory only).
>
> **Spot quota:** make sure the subscription has *Spot* vCPU quota for the F-series in your
> region (Portal → *Quotas*), or run with `--force-dedicated` (needs standard quota).

Then install the controller deps and fill in `config.yaml`:

```bash
pip install -r requirements.txt
$EDITOR config.yaml   # set resource_group, region, storage.account, compute.managed_identity
```

---

## Run it

```bash
# dry run: prints the itemized cost ceiling + the resources it WOULD create, makes nothing
python offload.py run --dry-run

# real run on the bundled MuJoCo sample (72 scenarios), with hard caps
python offload.py run --bundle sample_bundle --max-budget 5 --max-wall-clock 60

# non-interactive (skip the cost confirmation)
python offload.py run --bundle sample_bundle --yes

# force a dedicated (non-spot) VM
python offload.py run --force-dedicated
```

### Cheap test run (Poland Central)

To exercise the whole pipeline (provision → watchdog → monitor → teardown) for a few
cents instead of paying for 72 vCPUs, run on the cheapest **spot** VM in Poland Central.
It's an `F2s_v2` (2 vCPU / 4 GiB, ~**$0.018/hr** spot) — same F-family + spot/eviction
path as production, just small:

```bash
python offload.py run \
  --region polandcentral \
  --vm-size Standard_F2s_v2 \
  --nproc 2 \
  --max-budget 1 --max-wall-clock 25 \
  --bundle sample_bundle
```

Expect ~5–8 min end-to-end (boot + pip install + the 72-scenario sweep on 2 cores),
well under the caps; the itemized estimate will read well under $1.

| Poland Central, cheapest | size | vCPU/RAM | price |
|---|---|---|---|
| **Spot (default)** ✅ | `Standard_F2s_v2` | 2 / 4 GiB | ~$0.018/hr |
| Dedicated (`--force-dedicated`) | `Standard_B2s` | 2 / 4 GiB | ~$0.048/hr |
| Absolute cheapest (1 GiB — may OOM installing mujoco) | `Standard_B1s` | 1 / 1 GiB | ~$0.012/hr |

> Notes: **B-series has no Spot offering in Poland Central** (so spot picks the F/D
> families). Your storage account + managed identity can stay in whatever region they're
> already in — only the VM moves. Prices shown are live from the Retail Prices API on
> 2026-06; the tool always re-checks at launch.

You'll see a live status line, e.g.:

```
[  3.2m] running                41/72 (56.9%) fail=0 13.4/min ETA 2.3m cost~$0.41
```

ETA is computed from **measured** completed-scenarios-per-minute, refreshed every poll.

On completion (or budget / wall-clock / stuck / error / Ctrl-C) the tool tears everything
down and prints a one-line summary plus the **exact local results path**:

```
SUMMARY  run-id=mjoff-20260628-...  status=success  wall=6.1m  approx_cost=$0.42
         results: /home/you/mujoco-claude-azure-offload/results/mjoff-20260628-...
```

### Always-clean-up commands

```bash
# remove everything a specific run created (no local state needed — works after a crash)
python offload.py teardown-only --run-id mjoff-20260628-... --purge-blobs

# find & remove EVERYTHING this tool ever created in the resource group
python offload.py teardown-only --all --purge-blobs

# re-attach to a running job's progress (e.g. laptop slept)
python offload.py status --run-id mjoff-20260628-...

# independent manual verification that nothing is left:
az resource list --tag mjoff-tool=mujoco-azure-offload -o table
```

---

## The job bundle contract

A bundle is a folder containing:

- `job.py` exposing **`run_scenario(scenario: dict, workdir: str) -> dict`**
  (module/function names configurable in `config.yaml`).
- `scenarios.json` — either a top-level list or `{"scenarios": [ ... ]}`.
  Each element is one parallel task.
- `requirements.txt` — installed into an isolated venv on the VM (e.g. `numpy`, `mujoco`).

`run_scenario` is called once per scenario across all cores. Drop any artifacts
(`.json`/`.png`/`.mp4`/logs) into `workdir`; they're packed into the downloaded results.
Return a small JSON-serializable summary. See `sample_bundle/` for a complete example
(a damped-pendulum sweep that also renders a PNG and, if offscreen GL is available, an MP4).

> **Headless rendering:** the VM installs `libegl1`/`libosmesa6`/`ffmpeg` and sets
> `MUJOCO_GL` (default `egl`). The sample guards rendering so a scenario still succeeds if
> GL isn't usable. Set `compute.mujoco_gl: disable` to skip rendering entirely.

---

## Config & overrides

Everything lives in `config.yaml`; any field can be overridden on the CLI
(`--resource-group`, `--region`, `--account`, `--vm-size`, `--nproc`,
`--managed-identity`, `--max-budget`, `--max-wall-clock`, `--stuck-timeout`,
`--force-dedicated`, `--container`, `--subscription`). See `python offload.py run --help`.

## What gets created / billed per run

| Resource | Name | Cleanup |
|---|---|---|
| Spot VM | `mjoff-<ts>-<rand>` | deallocated by watchdog; deleted by teardown |
| OS disk | `…-osdisk` | `delete-option=Delete` (cascades) + teardown by name |
| NIC | `<vm>…` | `delete-option=Delete` (cascades with VM) |
| Public IP | `…-ip` | teardown by name |
| NSG (no inbound) | `…-nsg` | teardown by name |
| Blobs | `runs/<run-id>/…` | purged after download / `--purge-blobs` |

All are tagged `mjoff-run-id=<run-id>` and `mjoff-tool=mujoco-azure-offload`.

## Layout

```
offload.py                  # launcher (python offload.py ... | python -m azoffload ...)
config.yaml                 # single config file
azoffload/
  cli.py                    # orchestration: run / teardown-only / status
  config.py  naming.py      # config + run-id/tags/names
  pricing.py                # live Retail Prices API (+ fallback)
  storage.py  azcli.py      # blob data-plane (SDK) + az control-plane wrappers
  cloudinit.py  vm.py       # render customData + az vm lifecycle
  monitor.py  teardown.py   # poll/ETA/abort + idempotent teardown
  remote/runner.py          # on-VM orchestrator (progress/heartbeat to Blob)
  remote/worker.py          # runs one scenario in the job venv
  remote/watchdog.sh        # cloud-side self-destruct (IMDS + REST deallocate)
sample_bundle/              # runnable MuJoCo example (job.py, scenarios.json, requirements.txt)
tests/                      # offline suite (real runner.py vs. a fake Azure SDK)
```

## Install

```bash
git clone https://github.com/bidney/mujoco-azure-offload
cd mujoco-azure-offload
pip install -e .            # provides the `offload` console script
# or, without installing:
pip install -r requirements.txt && python offload.py --help
```

You also need the **Azure CLI** (`az`) on your PATH and an interactive `az login`.

## Tests

Fully offline — the suite runs the real on-VM `runner.py` against a filesystem-backed
fake Azure SDK, so no Azure account is required.

```bash
python tests/run_tests.py                                   # unit + security (integration auto-skips)
python -m venv jobenv && ./jobenv/bin/pip install numpy mujoco
MJOFF_TEST_JOBENV=$PWD/jobenv python tests/run_tests.py      # + real-MuJoCo integration
```

## Security

No secrets are prompted for or stored: the controller reuses your `az login`, and the VM
uses a managed identity (no keys in cloud-init). The VM has **no inbound ports**, env
values are shell-quoted, and archive extraction is path-traversal-guarded.

**Do not commit real credentials.** `config.yaml` here is placeholders only; put real
values in a git-ignored `config.local.yaml` (`--config config.local.yaml`) or pass them
as CLI flags. See [SECURITY.md](SECURITY.md).

## Disclaimer

This software provisions billable Azure resources. While it is built around hard
cost-safety guarantees (budget/wall-clock caps, an independent cloud-side self-destruct,
and idempotent teardown), **you are responsible for your Azure spend**. The live Azure
control plane has not yet been exercised in an automated end-to-end run in this repo;
start with `--dry-run` and a small `--max-budget` / `--max-wall-clock` run, and confirm
`az resource list --tag mjoff-tool=mujoco-azure-offload -o table` is empty afterward.
Provided "as is" under the MIT License, without warranty.

## License

[MIT](LICENSE) © 2026 Eugene Bidney
