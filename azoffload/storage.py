"""Blob Storage data-plane via the SDK, reusing the `az login` credential.

Auth modes:
  rbac : AzureCliCredential (your az-login identity). Requires the
         'Storage Blob Data Contributor' role on the storage account.
  key  : account key fetched at runtime via `az storage account keys list`
         (control-plane only; held in memory, never written to disk).
  auto : try rbac, fall back to key if the data-plane denies access.

Secrets are never prompted for nor persisted.
"""
import json
import os
import tarfile

from azure.identity import AzureCliCredential
from azure.storage.blob import BlobServiceClient

from . import azcli


def _service_rbac(account_url: str) -> BlobServiceClient:
    return BlobServiceClient(account_url, credential=AzureCliCredential())


def _service_key(account: str, account_url: str) -> BlobServiceClient:
    key = azcli.json_out(
        ["storage", "account", "keys", "list", "-n", account, "--query", "[0].value"]
    )
    return BlobServiceClient(account_url, credential=key)


def make_service(account: str, account_url: str, container: str, auth_mode: str):
    """Return (BlobServiceClient, mode_label)."""
    if auth_mode == "key":
        return _service_key(account, account_url), "account-key(via az)"

    svc = _service_rbac(account_url)
    if auth_mode == "rbac":
        return svc, "rbac(az-login)"

    # auto: probe data-plane; fall back to key on an authorization failure.
    try:
        cc = svc.get_container_client(container)
        try:
            cc.get_container_properties()
        except Exception as e:
            if "ContainerNotFound" not in str(e) and "does not exist" not in str(e):
                raise
        return svc, "rbac(az-login)"
    except Exception:
        return _service_key(account, account_url), "account-key(via az)"


class Store:
    def __init__(self, svc: BlobServiceClient, container: str, prefix: str):
        self.cc = svc.get_container_client(container)
        self.prefix = prefix

    def _name(self, blob: str) -> str:
        return f"{self.prefix}/{blob}"

    def ensure_container(self):
        try:
            self.cc.create_container()
        except Exception as e:
            if "ContainerAlreadyExists" not in str(e):
                # Most likely already exists; surface only if a real error.
                if "already exists" not in str(e).lower():
                    raise

    def upload_file(self, blob: str, path: str):
        with open(path, "rb") as f:
            self.cc.upload_blob(name=self._name(blob), data=f, overwrite=True)

    def get_json(self, blob: str):
        try:
            data = self.cc.download_blob(self._name(blob)).readall()
            return json.loads(data)
        except Exception:
            return None

    def download(self, blob: str, dest: str) -> bool:
        try:
            data = self.cc.download_blob(self._name(blob)).readall()
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            return True
        except Exception:
            return False

    def purge_prefix(self) -> int:
        n = 0
        try:
            for b in self.cc.list_blobs(name_starts_with=self.prefix + "/"):
                try:
                    self.cc.delete_blob(b.name)
                    n += 1
                except Exception:
                    pass
        except Exception:
            pass
        return n


def make_bundle(src_dir: str, dest_tar: str):
    """tar.gz the bundle folder's *contents* (so files land at the archive root)."""
    with tarfile.open(dest_tar, "w:gz") as t:
        for name in sorted(os.listdir(src_dir)):
            t.add(os.path.join(src_dir, name), arcname=name)


def _within(base: str, target: str) -> bool:
    base = os.path.realpath(base)
    target = os.path.realpath(target)
    return target == base or target.startswith(base + os.sep)


def safe_extractall(tar: tarfile.TarFile, dest_dir: str):
    """Extract guarding against path traversal (CVE-2007-4559 class). Rejects
    absolute paths / '..' escapes and skips symlinks/hardlinks (which a malicious
    or buggy job could use to write outside the results dir)."""
    dest_dir = os.path.realpath(dest_dir)
    for m in tar.getmembers():
        if os.path.isabs(m.name) or m.name.startswith(("/", "\\")):
            raise ValueError(f"unsafe absolute path in archive: {m.name!r}")
        if m.issym() or m.islnk():
            continue
        if not _within(dest_dir, os.path.join(dest_dir, m.name)):
            raise ValueError(f"path traversal in archive: {m.name!r}")
        tar.extract(m, dest_dir)


def extract_results(tar_path: str, dest_dir: str):
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(tar_path) as t:
        safe_extractall(t, dest_dir)
