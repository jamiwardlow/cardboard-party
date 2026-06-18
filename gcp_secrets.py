"""
Fetch sensitive config from Google Secret Manager.

Resolution order for a given name:
  1. An environment variable of the same name (used for local dev / overrides).
  2. Google Secret Manager, secret `name`, version `latest`, in the project
     given by GOOGLE_CLOUD_PROJECT (set automatically on App Engine).

Returns '' if neither is available, so local runs without the secret don't crash
at import time.
"""

import os
from functools import lru_cache


@lru_cache(maxsize=None)
def get_secret(name: str) -> str:
    env_val = os.environ.get(name)
    if env_val:
        return env_val

    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        return ''

    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        path = f'projects/{project}/secrets/{name}/versions/latest'
        response = client.access_secret_version(name=path)
        return response.payload.data.decode('utf-8')
    except Exception as e:
        # Missing or inaccessible secret → '' (don't crash startup over an
        # optional secret, e.g. DISCORD_CLIENT_SECRET before it's configured).
        print(f'get_secret({name}) unavailable: {e}')
        return ''
