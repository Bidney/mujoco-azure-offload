"""Single-file config (config.yaml) + CLI overrides, flattened into Settings."""
from dataclasses import dataclass, field

import yaml


@dataclass
class Settings:
    # azure
    subscription_id: str = ""
    resource_group: str = ""
    region: str = ""
    # storage
    account: str = ""
    container: str = "mjoff"
    storage_auth: str = "auto"          # auto | rbac | key
    # compute
    vm_size: str = "Standard_F72s_v2"
    image: str = "Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest"
    spot: bool = True                   # flipped off by --force-dedicated
    max_spot_price: float = -1          # -1 = pay up to on-demand (evict on capacity only)
    # OPTIONAL: resource id of a user-assigned identity. Leave blank to let the tool
    # give the VM a system-assigned identity and auto-grant it tightly-scoped roles.
    managed_identity: str = ""
    os_disk_size_gb: int = 64
    mujoco_gl: str = "osmesa"           # osmesa | egl | disable
    nproc: int = 0                      # 0 = all cores on the VM
    # named machine tiers selected by --cheap / --moderate / --expensive
    tiers: dict = field(default_factory=lambda: {
        "cheap": "Standard_F2s_v2",
        "moderate": "Standard_F16s_v2",
        "expensive": "Standard_F72s_v2",
    })
    # job
    job_module: str = "job"
    entry_function: str = "run_scenario"
    scenarios_file: str = "scenarios.json"
    # limits (hard cost-safety knobs)
    max_budget_usd: float = 10.0
    max_wall_clock_min: float = 120
    stuck_timeout_min: float = 10
    boot_timeout_min: float = 15
    poll_interval_sec: int = 15
    heartbeat_interval_sec: int = 10
    # pricing fallbacks (used only if Retail Prices API is unreachable / rate-limited).
    # Per-vCPU rates are scaled by the size's vCPU count so the estimate is sane for any
    # size (not just F72). The absolute *_hourly values are a last resort if the vCPU
    # count can't be parsed from the size name.
    fallback_per_vcpu_hour: float = 0.05        # ~F/D-series on-demand per vCPU-hour
    fallback_per_vcpu_hour_spot: float = 0.012  # ~F/D-series spot per vCPU-hour
    fallback_hourly_usd: float = 3.045
    fallback_spot_hourly_usd: float = 0.30
    disk_hourly_usd: float = 0.012      # ~64GB premium SSD
    ip_hourly_usd: float = 0.005        # standard public IP
    storage_flat_usd: float = 0.02      # blob storage + egress for <100MB
    # tags
    owner: str = ""

    @property
    def account_url(self) -> str:
        return f"https://{self.account}.blob.core.windows.net"


_PATHS = {
    "azure.subscription_id": "subscription_id",
    "azure.resource_group": "resource_group",
    "azure.region": "region",
    "storage.account": "account",
    "storage.container": "container",
    "storage.auth_mode": "storage_auth",
    "compute.vm_size": "vm_size",
    "compute.image": "image",
    "compute.spot": "spot",
    "compute.max_spot_price": "max_spot_price",
    "compute.managed_identity": "managed_identity",
    "compute.os_disk_size_gb": "os_disk_size_gb",
    "compute.mujoco_gl": "mujoco_gl",
    "compute.nproc": "nproc",
    "job.job_module": "job_module",
    "job.entry_function": "entry_function",
    "job.scenarios_file": "scenarios_file",
    "limits.max_budget_usd": "max_budget_usd",
    "limits.max_wall_clock_min": "max_wall_clock_min",
    "limits.stuck_timeout_min": "stuck_timeout_min",
    "limits.boot_timeout_min": "boot_timeout_min",
    "limits.poll_interval_sec": "poll_interval_sec",
    "limits.heartbeat_interval_sec": "heartbeat_interval_sec",
    "pricing.fallback_per_vcpu_hour": "fallback_per_vcpu_hour",
    "pricing.fallback_per_vcpu_hour_spot": "fallback_per_vcpu_hour_spot",
    "pricing.fallback_hourly_usd": "fallback_hourly_usd",
    "pricing.fallback_spot_hourly_usd": "fallback_spot_hourly_usd",
    "pricing.disk_hourly_usd": "disk_hourly_usd",
    "pricing.ip_hourly_usd": "ip_hourly_usd",
    "pricing.storage_flat_usd": "storage_flat_usd",
    "tags.owner": "owner",
}


def _dig(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def load(path: str) -> Settings:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    s = Settings()
    for dotted, attr in _PATHS.items():
        val = _dig(raw, dotted)
        if val is not None:
            setattr(s, attr, val)
    if isinstance(raw.get("tiers"), dict):
        s.tiers = {**s.tiers, **{k: v for k, v in raw["tiers"].items() if v}}
    return s


def apply_overrides(s: Settings, args) -> Settings:
    """Apply only the CLI flags the user actually set (argparse default=None)."""
    m = {
        "subscription": "subscription_id",
        "resource_group": "resource_group",
        "region": "region",
        "account": "account",
        "container": "container",
        "vm_size": "vm_size",
        "nproc": "nproc",
        "managed_identity": "managed_identity",
        "max_budget": "max_budget_usd",
        "max_wall_clock": "max_wall_clock_min",
        "stuck_timeout": "stuck_timeout_min",
    }
    # tier (--cheap/--moderate/--expensive) sets vm_size; an explicit --vm-size wins,
    # so apply the tier first and let the loop's vm_size override it.
    tier = getattr(args, "tier", None)
    if tier:
        s.vm_size = s.tiers.get(tier, s.vm_size)
    for arg_name, attr in m.items():
        val = getattr(args, arg_name, None)
        if val is not None:
            setattr(s, attr, val)
    if getattr(args, "force_dedicated", False):
        s.spot = False
    return s


def validate_for_run(s: Settings) -> list:
    """Return a list of human-readable problems (empty = OK to launch)."""
    problems = []
    if not s.resource_group:
        problems.append("azure.resource_group is required")
    if not s.account:
        problems.append("storage.account is required")
    if not s.region:
        problems.append("azure.region is required")
    # managed_identity is OPTIONAL — blank means system-assigned + auto-roles.
    if s.max_budget_usd <= 0:
        problems.append("limits.max_budget_usd must be > 0")
    if s.max_wall_clock_min <= 0:
        problems.append("limits.max_wall_clock_min must be > 0")
    return problems
