# Security

## Reporting a vulnerability

Please open a private security advisory on GitHub
(*Security → Advisories → Report a vulnerability*) or email the maintainer rather than
filing a public issue. You'll get a response as soon as practical.

## Security model

This tool is designed so that **no secrets are ever prompted for, written to disk, or
embedded in cloud resources**:

- **Local controller** reuses your interactive `az login` credential
  (`AzureCliCredential`). If blob data-plane RBAC is unavailable it may fall back to the
  storage account key, which is fetched at runtime via `az` and held **in memory only** —
  never written to disk or logged.
- **On the VM**, the job and the self-destruct watchdog authenticate with the VM's
  **managed identity** (system-assigned by default; tokens via IMDS). No keys, SAS tokens,
  or connection strings are placed in cloud-init or environment files. By default the tool
  grants that identity two **tightly-scoped** roles — *Virtual Machine Contributor* on
  **only that VM** (self-deallocate) and *Storage Blob Data Contributor* on **only that
  run's container** (blob I/O).
- **Network exposure** is minimised: the VM is created with **no inbound NSG rules**
  (`--nsg-rule NONE`) — no SSH, no open ports. It is outbound-only.
- **Cloud-init env values are `shlex`-quoted** so a config value cannot inject shell into
  the watchdog/runner that source the env file.
- **Archive extraction is path-traversal-guarded** (rejects `..`/absolute paths, skips
  symlinks) for both the uploaded bundle and the downloaded results.

See the README's "How the cloud-side self-destruct works" and the security tests in
`tests/run_tests.py` (`cloudinit_blocks_shell_injection`, `safe_extract_blocks_traversal`,
`no_shell_true_in_source`).

## Do NOT commit secrets

`config.yaml` in this repo contains **placeholders only**. When you configure the tool
for your environment:

- Put real values (subscription id, storage account, managed-identity resource id) in a
  **`config.local.yaml`** (git-ignored) and run with `--config config.local.yaml`, or
  pass them as CLI flags — do not paste them into the committed `config.yaml`.
- `.gitignore` blocks `*.env`, `*.pem`, `*.key`, `config.local.yaml`, `.azure/` and
  similar. Double-check `git status` before committing.

## Least-privilege notes

- **Default (system-assigned):** roles are scoped to a single VM and a single container,
  so the identity can't touch any other resource — the tightest practical grant. The
  trade-off is that *your* launching login must be able to create role assignments
  (Owner / User Access Administrator).
- **User-assigned override (restricted environments):** the documented pre-created
  identity is granted *Virtual Machine Contributor* at **resource-group** scope (broader,
  because the VM name isn't known until creation). If you use this path, prefer a
  **dedicated resource group** for offload runs, or a **custom role** limited to
  `Microsoft.Compute/virtualMachines/deallocate/action` (+ read).
