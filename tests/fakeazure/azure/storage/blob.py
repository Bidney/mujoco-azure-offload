"""Filesystem-backed fake of the bits of azure-storage-blob the tool uses.

Blobs live under $BLOB_ROOT/<container>/<blob-name>. Good enough to exercise
storage.Store (controller) and runner.py (on-VM) end-to-end without Azure.
"""
import os


def _root():
    return os.environ["BLOB_ROOT"]


class _Downloaded:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data


class _BlobItem:
    def __init__(self, name):
        self.name = name


class ContainerClient:
    def __init__(self, account_url=None, container_name=None, credential=None):
        self.container = container_name or "default"
        self.root = os.path.join(_root(), self.container)

    def _path(self, name):
        return os.path.join(self.root, name)

    def create_container(self):
        if os.path.isdir(self.root):
            raise Exception("ContainerAlreadyExists")
        os.makedirs(self.root)

    def get_container_properties(self):
        if not os.path.isdir(self.root):
            raise Exception("ContainerNotFound: The specified container does not exist.")
        return {"name": self.container}

    def upload_blob(self, name=None, data=None, overwrite=False):
        p = self._path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if hasattr(data, "read"):
            data = data.read()
        if isinstance(data, str):
            data = data.encode()
        with open(p, "wb") as f:
            f.write(data)

    def download_blob(self, name):
        p = self._path(name)
        if not os.path.exists(p):
            raise Exception(f"ResourceNotFound: blob {name} not found")
        with open(p, "rb") as f:
            return _Downloaded(f.read())

    def list_blobs(self, name_starts_with=None):
        if not os.path.isdir(self.root):
            return
        for dirpath, _, files in os.walk(self.root):
            for fn in files:
                rel = os.path.relpath(os.path.join(dirpath, fn), self.root).replace(os.sep, "/")
                if name_starts_with and not rel.startswith(name_starts_with):
                    continue
                yield _BlobItem(rel)

    def delete_blob(self, name):
        nm = name.name if hasattr(name, "name") else name
        p = self._path(nm)
        if os.path.exists(p):
            os.remove(p)


class BlobServiceClient:
    def __init__(self, account_url=None, credential=None):
        self.account_url = account_url

    def get_container_client(self, container):
        return ContainerClient(self.account_url, container)
