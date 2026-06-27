# Runbook — testing on a fresh machine (VS Code)

A start-to-finish guide to running `mujoco-azure-offload` against a real Azure
subscription, from cloning the repo to verifying nothing is left billing.

Nothing is hardcoded: `run` asks you to confirm the **subscription**, **resource
group**, **storage account**, and **machine** before it creates anything.

---

## 0. Prerequisites

- **Azure CLI** (`az`) and **Python 3.9+** on the machine.
- An Azure subscription where you can **create VMs**. The default (system-assigned)
  identity path also needs you to be able to **create role assignments** (Owner /
  User Access Administrator / subscription account admin). If you can't, see
  [Restricted environments](#restricted-environments-no-role-assignment-rights).
- *Spot* vCPU quota for the F-series in your region (Portal → *Quotas*), or use
  `--force-dedicated`.

---

## 1. One-time Azure setup

The tool manages everything ephemeral, but the **resource group and storage account
must already exist** (it never creates those). Do this once:

```bash
az login                                  # then pick the subscription you'll use
az account set --subscription "<SUBSCRIPTION_ID>"

RG=mjoff-rg
LOC=polandcentral
SA=mjoffstore$RANDOM                       # storage names are global + lowercase

az group create -n $RG -l $LOC
az storage account create -n $SA -g $RG -l $LOC --sku Standard_LRS

# let YOUR login read/write blobs over RBAC (so the controller needs no account key)
SUB=$(az account show --query id -o tsv)
ME=$(az ad signed-in-user show --query id -o tsv)
az role assignment create --assignee-object-id $ME --assignee-principal-type User \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$SA"

echo "resource group: $RG"
echo "storage account: $SA"                # you'll type these at the run prompt
```

> No managed identity to create here. At launch the tool gives the VM a
> *system-assigned* identity and auto-grants it two tightly-scoped roles
> (deallocate on just that VM, blob on just that run's container).

---

## 2. Clone & set up in VS Code

```bash
git clone https://github.com/Bidney/mujoco-azure-offload
cd mujoco-azure-offload
code .                                     # open the folder in VS Code
```

Create the environment (VS Code: *Python: Create Environment → Venv*, or):

```bash
python -m venv .venv
. .venv/bin/activate                       # Windows: .venv\Scripts\activate
pip install -e .                           # provides the `offload` command + deps
```

> `pip install -e .` is important on a fresh machine — it ships the embedded
> on-VM runner/worker/watchdog with the package.

---

## 3. Prove the software runs (no Azure)

```bash
python tests/run_tests.py                  # unit + security tests (integration auto-skips)
```

Expect `RESULT: 15 passed`. To also run the real-MuJoCo integration:

```bash
python -m venv jobenv && ./jobenv/bin/pip install numpy mujoco
MJOFF_TEST_JOBENV=$PWD/jobenv python tests/run_tests.py
```

---

## 4. Log in to Azure

```bash
az login                                   # or: az login --use-device-code
az account show -o table                   # sanity-check the active subscription
```

The tool uses whatever your session has active, but always **asks you to confirm
the subscription** at run time, so you don't have to set it here.

---

## 5. Dry run — validates everything, creates nothing

```bash
python offload.py run --dry-run --cheap
```

It will prompt for subscription / resource group / storage account / machine, then
print the plan and the **itemized cost ceiling** from live pricing, and exit with
`No resources created.` This confirms code, deps, Azure auth, pricing and config all
wire together. (Add `--yes` + the flags to skip prompts; `--yes` requires an explicit
`--subscription`.)

---

## 6. Real run — small and capped

```bash
python offload.py run --cheap --max-budget 1 --max-wall-clock 25
```

At the prompts: confirm/paste the subscription, type your `RG` and storage account
from step 1, choose `cheap`. Review the itemized cost, confirm. Then:

- a `Standard_F2s_v2` spot VM is created (~$0.018/hr; worst case ~$0.02 for 25 min),
- it installs deps and runs the 72-scenario sample sweep across its cores,
- you get a live status line with a throughput-based ETA,
- on completion it **deallocates + deletes** the VM and downloads results, printing
  the exact local path.

Useful while it runs:
- re-attach to progress: `python offload.py status --run-id <id>`
- abort: `Ctrl-C` → it tears everything down on the way out.

---

## 7. Verify nothing is left billing

```bash
az resource list --tag mjoff-tool=mujoco-azure-offload -o table     # should be empty
```

In the Portal you can watch the VM go **Stopped (deallocated)** then disappear — that
deallocate (not just power-off) is what stops compute billing. Belt-and-suspenders
cleanup of any run, even after a crash or laptop death:

```bash
python offload.py teardown-only --all --purge-blobs
# or a specific run:
python offload.py teardown-only --run-id <id> --purge-blobs
```

The cloud-side watchdog also self-deallocates the VM at `min(--max-wall-clock,
--max-budget ÷ price)` even if your laptop dies mid-run.

---

## 8. Scale up to a real workload

```bash
python offload.py run --expensive --bundle /path/to/your/bundle \
  --max-budget 20 --max-wall-clock 120
```

`--expensive` is `Standard_F72s_v2` (72 vCPU). Remap the tiers in
`config.yaml → tiers`. Your bundle must expose
`run_scenario(scenario: dict, workdir: str) -> dict` — see `sample_bundle/` and the
[README job contract](README.md#the-job-bundle-contract).

---

## Restricted environments (no role-assignment rights)

If your login can create VMs but **not** role assignments, the default
system-assigned path will abort + tear down with a clear message. Have an Owner
pre-create a user-assigned identity once, then point the tool at it:

```bash
ID=mjoff-watchdog-identity
az identity create -n $ID -g $RG -l $LOC
PRINC=$(az identity show -n $ID -g $RG --query principalId -o tsv)
az role assignment create --assignee-object-id $PRINC --assignee-principal-type ServicePrincipal \
  --role "Virtual Machine Contributor" --scope "/subscriptions/$SUB/resourceGroups/$RG"
az role assignment create --assignee-object-id $PRINC --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$SA"

RESID=$(az identity show -n $ID -g $RG --query id -o tsv)
python offload.py run --cheap --managed-identity "$RESID" --max-budget 1 --max-wall-clock 25
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `cannot create role assignments … needs Owner` | Your login can't assign roles. Use a pre-created user-assigned identity (above). |
| `az vm create … SkuNotAvailable` / spot capacity | No spot capacity/quota for that size/region. Try another region/size or `--force-dedicated`. |
| 403 on blob right after setup | RBAC still propagating (~1-2 min). The VM-side reader already retries; for the controller, wait and retry. |
| `RG`/storage "not found" | They must pre-exist (step 1). The tool only creates ephemeral resources + the blob container. |
| `.mp4`/`.png` missing in results | Headless GL — default `mujoco_gl: osmesa` is correct for CPU VMs; rollouts/JSON still succeed. Check `scenario_*/_worker.log`. |
| Want to keep blobs after a run | `--keep-blobs` (otherwise purged after the results download). |

See [SECURITY.md](SECURITY.md) for the secrets/identity model and
[README.md](README.md) for the design and the cost-safety mechanism.
