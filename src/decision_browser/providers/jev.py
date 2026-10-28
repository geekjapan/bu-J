from __future__ import annotations

import json

import httpx

from decision_browser.providers.systemone import ProviderError, SystemOneProvider, parse_response


class JevProvider(SystemOneProvider):
    BASE_URL = 'https://api.typesafe.ai'

    def __init__(self, api_key: str, model: str = 'jev-1.14.0', *,
                 transport: httpx.AsyncBaseTransport | None = None, max_retries: int = 1):
        if not api_key or '\n' in api_key or '\r' in api_key:
            raise ValueError('invalid API key')
        if model != 'jev-1.14.0':
            raise ValueError('model must be reviewed and pinned')
        super().__init__(base_url=self.BASE_URL, path='/v1/systemone',
                         headers={'Authorization': f'Bearer {api_key}'}, model=model,
                         deployment=model, provider='typesafe', transport=transport,
                         max_retries=max_retries)

    async def available_models(self, deadline: float) -> tuple[str, ...]:
        raw = await self._request('GET', '/v1/models', None, deadline)
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('models'), list):
            raise ProviderError('invalid model list')
        names = tuple(item['name'] for item in data['models']
                      if isinstance(item, dict) and isinstance(item.get('name'), str))
        if self.model not in names:
            raise ProviderError('pinned model unavailable')
        return names

