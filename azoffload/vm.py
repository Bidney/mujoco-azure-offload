"""VM lifecycle via `az`. One `az vm create` builds the VM + NIC + public IP +
NSG + OS disk, attaches the managed identity, passes custom-data, and tags
everything. NIC and OS disk are created with delete-option=Delete so deleting
the VM cascades to them; the public IP + NSG are deleted by name in teardown.

Identity: by default the VM gets a SYSTEM-assigned identity and the tool grants
it two tightly-scoped roles (deallocate on this VM, blob on this run's container)
so nothing identity-related needs to live in config. If compute.managed_identity
is set, that user-assigned identity is used instead and no roles are assigned.
"""
import time

from . import azcli


def create_vm(s, run_id, names, tags_dict, custom_data_path, system_identity=True):
    identity = "[system]" if system_identity else s.managed_identity
    args = [
        "vm", "create",
        "-g", s.resource_group,
        "-n", names["vm"],
        "--location", s.region,
        "--image", s.image,
        "--size", s.vm_size,
        "--admin-username", "mjoff",
        "--generate-ssh-keys",
        "--public-ip-sku", "Standard",
        "--public-ip-address", names["ip"],
        "--nsg", names["nsg"],
        "--nsg-rule", "NONE",                  # no inbound; outbound-only
        "--os-disk-name", names["disk"],
        "--os-disk-size-gb", str(s.os_disk_size_gb),
        "--nic-delete-option", "Delete",
        "--os-disk-delete-option", "Delete",
        "--custom-data", custom_data_path,
        "--assign-identity", identity,
    ]
    if s.spot:
        args += [
            "--priority", "Spot",
            "--eviction-policy", "Delete",     # eviction also self-cleans
            f"--max-price={s.max_spot_price}",  # '=' form: -1 isn't misread as a flag
        ]
    args += ["--tags"] + [f"{k}={v}" for k, v in tags_dict.items()]
    return azcli.json_out(args)


def vm_exists(s, names) -> bool:
    out = azcli.safe_json(["vm", "show", "-g", s.resource_group, "-n", names["vm"]])
    return out is not None


def get_identity_client_id(resource_id: str) -> str:
    """clientId of a user-assigned identity (needed by IMDS/ManagedIdentityCredential
    to pick the right identity on the VM). Returns '' on failure."""
    cid = azcli.safe_json(["identity", "show", "--ids", resource_id, "--query", "clientId"])
    return cid or ""


def get_principal_id(s, names, create_output=None) -> str:
    """principalId of the VM's system-assigned identity."""
    if create_output:
        pid = (create_output.get("identity") or {}).get("principalId")
        if pid:
            return pid
    return azcli.safe_json(
        ["vm", "show", "-g", s.resource_group, "-n", names["vm"], "--query", "identity.principalId"]
    ) or ""


def _assign_role(principal_id: str, role: str, scope: str):
    """Create one role assignment, tolerating AAD replication lag, surfacing a
    clear PermissionError if the caller simply isn't allowed to assign roles."""
    for attempt in range(6):
        rc, err = azcli.try_run([
            "role", "assignment", "create",
            "--assignee-object-id", principal_id,
            "--assignee-principal-type", "ServicePrincipal",
            "--role", role, "--scope", scope,
        ])
        e = err.lower()
        if rc == 0 or "roleassignmentexists" in e or "already exists" in e:
            return
        if "principalnotfound" in e or "does not exist in the directory" in e:
            time.sleep(10)            # identity not yet replicated to AAD
            continue
        if "authorization" in e or "forbidden" in e or "does not have permission" in e \
                or "authorizationfailed" in e:
            raise PermissionError(
                f"your login cannot create role assignments for '{role}' "
                f"(needs Owner / User Access Administrator). {err.strip()[:200]}"
            )
        time.sleep(8)
    raise RuntimeError(f"failed to assign role '{role}' after retries")


def assign_identity_roles(s, sub_id, names):
    """Grant the VM's system-assigned identity least-privilege roles:
      - Virtual Machine Contributor scoped to ONLY this VM  -> self-deallocate
      - Storage Blob Data Contributor scoped to ONLY this run's container -> blob I/O
    Raises PermissionError/RuntimeError if it can't (caller must then tear down)."""
    principal_id = get_principal_id(s, names)
    if not principal_id:
        raise RuntimeError("could not read the VM's system-assigned identity principalId")
    vm_scope = (f"/subscriptions/{sub_id}/resourceGroups/{s.resource_group}"
                f"/providers/Microsoft.Compute/virtualMachines/{names['vm']}")
    container_scope = (f"/subscriptions/{sub_id}/resourceGroups/{s.resource_group}"
                       f"/providers/Microsoft.Storage/storageAccounts/{s.account}"
                       f"/blobServices/default/containers/{s.container}")
    _assign_role(principal_id, "Virtual Machine Contributor", vm_scope)
    _assign_role(principal_id, "Storage Blob Data Contributor", container_scope)
    return principal_id


def tag_aux_resources(s, names, tags_dict):
    """Best-effort: stamp the run-id tags on the auto-created public IP, NSG and
    OS disk too, so `teardown-only --all` can still discover them even if the VM
    itself is later evicted (which removes the VM's own tags). Never raises."""
    rg = s.resource_group
    tagargs = [f"{k}={v}" for k, v in tags_dict.items()]
    lookups = [
        ["network", "public-ip", "show", "-g", rg, "-n", names["ip"], "--query", "id"],
        ["network", "nsg", "show", "-g", rg, "-n", names["nsg"], "--query", "id"],
        ["disk", "show", "-g", rg, "-n", names["disk"], "--query", "id"],
    ]
    for args in lookups:
        rid = azcli.safe_json(args)
        if rid:
            azcli.try_run(["resource", "tag", "--ids", rid, "--is-incremental", "--tags"] + tagargs)
