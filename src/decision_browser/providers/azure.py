from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

from decision_browser.providers.systemone import SystemOneProvider


def validate_endpoint(endpoint: str) -> str:
    try:
        url = urlsplit(endpoint)
        valid = (url.scheme == 'https' and url.username is None and url.password is None
                 and url.port in (None, 443) and url.path in ('', '/')
                 and '?' not in endpoint and '#' not in endpoint and url.hostname is not None
                 and re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?'
                                  r'\.cognitiveservices\.azure\.com', url.hostname) is not None
                 and endpoint.isascii() and not any(c.isspace() for c in endpoint))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('endpoint must be an HTTPS Azure Cognitive Services resource origin')
    return f'https://{url.hostname}'


class AzureDecisionProvider(SystemOneProvider):
    MODEL = 'microsoft-decision-1'

    def __init__(self, endpoint: str, deployment: str = 'decision-1', *,
                 api_key: str | None = None, entra_token: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, max_retries: int = 1):
        base_url = validate_endpoint(endpoint)
        if (api_key is None) == (entra_token is None):
            raise ValueError('provide exactly one Azure API key or Entra token')
        secret = api_key if api_key is not None else entra_token
        if not secret or not secret.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in secret):
            raise ValueError('invalid Azure credential')
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', deployment):
            raise ValueError('invalid Azure deployment name')
        headers = {'api-key': secret} if api_key is not None else {'Authorization': f'Bearer {secret}'}
        super().__init__(base_url=base_url, path='/providers/microsoft/v1/systemone',
                         headers=headers, model=self.MODEL, deployment=deployment,
                         provider='azure', transport=transport, max_retries=max_retries)
