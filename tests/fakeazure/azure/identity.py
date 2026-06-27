"""Fake credentials — no network, no auth. Stand-ins for the real classes."""


class _Cred:
    def __init__(self, *a, **k):
        pass

    def get_token(self, *scopes, **k):
        class _T:
            token = "fake-token"
            expires_on = 9999999999
        return _T()


class AzureCliCredential(_Cred):
    pass


class DefaultAzureCredential(_Cred):
    pass
