"""Keep legacy GitHub state bytes; qualify new static GitLab state/claims."""

from contextlib import contextmanager


class SourceScopedState:
    def __init__(self, store, identity):
        self.store, self.identity = store, identity
        self.prefix = "source:v1:" + identity.canonical_key + ":"

    def get(self, key):
        return self.store.get(self.prefix + key)

    def put(self, key, value):
        return self.store.put(self.prefix + key, value)

    def delete(self, key):
        return self.store.delete(self.prefix + key)

    @contextmanager
    def claim(self, resource, operation):
        prefix = self.identity.path + ":"
        if not resource.startswith(prefix):
            raise ValueError("Claim repository differs from source identity")
        with self.store.claim(self.prefix + resource[len(prefix) :], operation):
            yield


def source_scoped_state(store, identity):
    return (
        store if identity.provider == "github" else SourceScopedState(store, identity)
    )
