"""VM lifecycle via `az`. One `az vm create` builds the VM + NIC + public IP +
NSG + OS disk, attaches the managed identity, passes custom-data, and tags
everything. NIC and OS disk are created with delete-option=Delete so deleting
the VM cascades to them; the public IP + NSG are deleted by name in teardown.
"""
from . import azcli


def create_vm(s, run_id, names, tags_dict, custom_data_path):
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
        "--assign-identity", s.managed_identity,
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
