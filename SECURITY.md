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
  **user-assigned managed identity** (tokens via IMDS). No keys, SAS tokens, or
  connection strings are placed in cloud-init or environment files.
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

## Least-privilege recommendation

The setup grants the VM's managed identity *Virtual Machine Contributor* on the resource
group so the watchdog can deallocate the VM. To tighten this, define a **custom role**
limited to `Microsoft.Compute/virtualMachines/deallocate/action` (+ read) and/or use a
**dedicated resource group** for offload runs so the identity can only affect ephemeral
resources.
