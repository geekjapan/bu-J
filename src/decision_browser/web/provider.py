"""Action space and question construction adapted from jev-ultrafast (MIT).
See provider-UPSTREAM.json for source hashes and license.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from urllib.parse import urlsplit

import httpx

from .._runtime import azure_cli_executable

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE
from .progress import progress


class ProviderError(RuntimeError):
    pass


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "progress": progress(page, goal, history),
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def _endpoint(endpoint, *, text=False):
    try:
        url = urlsplit(endpoint)
        valid = (endpoint.isascii() and not any(c.isspace() for c in endpoint)
                 and url.scheme == 'https' and url.username is None and url.password is None
                 and url.port in (None, 443) and '?' not in endpoint and '#' not in endpoint
                 and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,62}\.(?:cognitiveservices|openai)\.azure\.com', url.hostname or '')
                 and url.path in (('', '/', '/openai/v1', '/openai/v1/') if text else ('', '/')))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('endpoint must be an approved HTTPS Azure resource')
    return endpoint.rstrip('/')


def _azure_token():
    try:
        result = subprocess.run(
            [azure_cli_executable(), 'account', 'get-access-token', '--resource', 'https://cognitiveservices.azure.com/',
             '--query', 'accessToken', '--output', 'tsv'],
            capture_output=True, text=True, encoding='utf-8', check=True, timeout=25)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ProviderError('Azure CLI token acquisition failed') from None


def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_choice(answer, ids):
    try:
        probabilities = answer['probabilities']
        ranked = sorted(probabilities.values(), reverse=True)
        valid = (answer.get('type') == 'choice' and set(probabilities) == set(ids)
                 and all(_probability(v) for v in probabilities.values())
                 and _probability(answer['confidence'])
                 and abs(sum(ranked) - 1) <= 0.001
                 and answer['choice'] in ids and probabilities[answer['choice']] == ranked[0])
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        valid = False
    if not valid:
        raise ProviderError('invalid choice response')
    return answer


def _head_accepted(answer):
    ranked = sorted(answer['probabilities'].values(), reverse=True)
    return ranked[0] >= .90 and ranked[0] - (ranked[1] if len(ranked) > 1 else 0) >= .20


class ModelClient:
    MODEL = 'microsoft-decision-1'
    MAX_BYTES = 1_000_000

    def __init__(self, endpoint, deployment, text_endpoint, text_deployment, *,
                 max_calls=120, max_text_calls=20, token_supplier=None, transport=None):
        self.endpoint = _endpoint(endpoint)
        self.text_endpoint = _endpoint(text_endpoint, text=True)
        for name in (deployment, text_deployment):
            if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', name):
                raise ValueError('invalid Azure deployment name')
        if type(max_calls) is not int or not 0 <= max_calls <= 120:
            raise ValueError('decision call cap must be between 0 and 120')
        if type(max_text_calls) is not int or not 0 <= max_text_calls <= 20:
            raise ValueError('text call cap must be between 0 and 20')
        self.deployment, self.text_deployment = deployment, text_deployment
        self.max_calls, self.max_text_calls = max_calls, max_text_calls
        self.calls, self.text_calls = [], []
        self._token_supplier = token_supplier or _azure_token
        self._token = None
        self.client = httpx.Client(timeout=25, follow_redirects=False, trust_env=False,
                                   transport=transport or httpx.HTTPTransport(retries=0))

    field_context = staticmethod(field_context)

    @property
    def total_tokens(self):
        total = 0
        for call in self.calls + self.text_calls:
            usage = call.get('usage', {})
            count = usage.get('total_tokens')
            if type(count) is not int:
                count = usage.get('input_tokens', usage.get('prompt_tokens', 0)) + usage.get('output_tokens', usage.get('completion_tokens', 0))
            if type(count) is int and count >= 0:
                total += count
        return total

    def close(self):
        self.client.close()
        self._token = None

    def authenticate(self):
        token = self._token if self._token is not None else self._token_supplier()
        if not isinstance(token, str) or not token or not token.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in token):
            raise ProviderError('invalid Azure credential')
        self._token = token

    def _post(self, url, body, *, text=False):
        calls, limit = (self.text_calls, self.max_text_calls) if text else (self.calls, self.max_calls)
        if len(calls) >= limit:
            raise ProviderError('HTTP call budget exceeded')
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(encoded) > self.MAX_BYTES:
            raise ProviderError('request byte budget exceeded')
        self.authenticate()
        token = self._token
        record = {'request_digest': hashlib.sha256(encoded).hexdigest(), 'http_status': None,
                  'request_id': None, 'response': None, 'usage': {}}
        started = time.perf_counter()
        calls.append(record)
        try:
            with self.client.stream('POST', url, content=encoded,
                                    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}) as response:
                record['http_status'] = response.status_code
                request_id = response.headers.get('x-request-id', response.headers.get('apim-request-id', ''))
                record['request_id'] = request_id.replace(token, '[REDACTED]')[:256]
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > self.MAX_BYTES:
                        raise ProviderError('response byte budget exceeded')
                safe = data.decode('utf-8', errors='replace').replace(token, '[REDACTED]')
                record['response'] = safe
                if response.status_code != 200:
                    raise ProviderError(f'HTTP status {response.status_code}')
                result = json.loads(safe)
                if not isinstance(result, dict):
                    raise ValueError()
                usage = result.get('usage', {})
                if not isinstance(usage, dict) or any(type(usage[k]) is not int or usage[k] < 0
                        for k in ('total_tokens', 'input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens') if k in usage):
                    raise ValueError()
                record['usage'] = usage
                return result, record
        except (httpx.HTTPError, ValueError, TypeError):
            raise ProviderError('model request failed or invalid JSON response') from None
        finally:
            record['latency_ms'] = round((time.perf_counter() - started) * 1000)

    def choose(self, page, goal, history):
        observed_progress = progress(page, goal, history)
        instruction = goal + '\nNext required step: ' + observed_progress['next_step'] + '\n' + NEXT_ACTION
        elements, targets, controls = action_space(page['actions'])
        labels = {'CLICK': 'Click an element, button, menu option, autocomplete suggestion, or calendar day.',
                  'TYPE_TEXT': 'Enter or replace text in an editable field. A small LLM will supply the value from the goal.',
                  'SELECT': 'Select an observed dropdown value.'}
        operations = {key: labels[key] for key in targets}
        operations.update({key: value['label'] for key, value in controls.items()})
        operations.update(DONE='Every requirement is visibly satisfied.', BLOCKED='No supported operation can progress.')
        questions = {'operation': {'type': 'choice', 'criteria': operations,
                                  'instructions': instruction},
                     'state_supported': {'type': 'noul',
                         'instructions': 'Does the observed page provide at least one visible browser control for making progress on the goal, or visible evidence that the goal is complete? This asks about the current page, not whether all future steps can already be completed.',
                         'criteria': {'true': 'Visible observed inputs, buttons, options or links can advance the goal, or visible result evidence supports DONE.',
                                      'false': 'Page is an error, login, captcha, or contains no usable controls for the goal.'}}}
        for operation, candidates in targets.items():
            questions[operation.lower() + '_target'] = {
                'type': 'choice', 'criteria': {
                    index: {'element': f"[{index}] {a['label']}",
                            'current_value': a.get('current_value', a.get('value', '')),
                            **{k: a[k] for k in ('role', 'checked', 'selected', 'expanded') if k in a}}
                    for index, a in candidates.items()},
                'instructions': instruction + '\nChoose a target for ' + operation + '.\n' + TARGET}
            if len(candidates) == 1:
                questions[operation.lower() + '_target']['criteria']['__none__'] = {
                    'element': 'No listed target is appropriate; stop without an action.', 'current_value': ''}
        body = {'model': self.deployment, 'state': {'goal': goal, 'progress': observed_progress,
            'page': {k: page[k] for k in ('url', 'title', 'text')}, 'elements': elements,
            'recent_actions': [{k: h.get(k) for k in ('action', 'kind', 'text', 'page_changed')} for h in history[-10:]]},
            'questions': questions}
        result, record = self._post(self.endpoint + '/providers/microsoft/v1/systemone', body)
        try:
            if result['model'] != self.MODEL:
                raise ProviderError('unapproved resolved model')
            answers = result['answers']
            operation_answer = validate_choice(answers.get('operation', {}), operations)
            support = answers['state_supported']
            if support.get('type') != 'noul' or not _probability(support['noul']):
                raise ProviderError('invalid state support response')
            operation = operation_answer['choice']
            target, target_answer = None, None
            if operation in targets:
                ids = set(questions[operation.lower() + '_target']['criteria'])
                target_answer = validate_choice(answers.get(operation.lower() + '_target', {}), ids)
                target = target_answer['choice']
                choice = 'BLOCKED' if target == '__none__' else targets[operation][target]['id']
                probabilities = {a['id']: target_answer['probabilities'][index] for index, a in targets[operation].items()}
                if '__none__' in ids:
                    probabilities['BLOCKED'] = target_answer['probabilities']['__none__']
            else:
                choice = controls[operation]['id'] if operation in controls else operation
                probabilities = {choice: operation_answer['probabilities'][operation]}
            accepted = (target != '__none__' and _head_accepted(operation_answer) and (target_answer is None or _head_accepted(target_answer))
                        and support['noul'] >= .95)
        except (KeyError, TypeError, AttributeError):
            raise ProviderError('invalid decision response') from None
        return {'choice': choice, 'operation': operation, 'target': target,
                'confidence': operation_answer['confidence'], 'probabilities': probabilities,
                'operation_probabilities': operation_answer['probabilities'],
                'target_probabilities': target_answer['probabilities'] if target_answer else {},
                'target_confidence': target_answer['confidence'] if target_answer else None,
                'raw_answers': answers, 'model': result['model'], 'usage': record['usage'],
                'latency_ms': record['latency_ms'], 'request': body, 'accepted': accepted,
                'reason': 'accepted' if accepted else 'decision confidence, margin or state support below gate'}

    def field_text(self, context):
        body = {'model': self.text_deployment, 'max_tokens': 256, 'temperature': 0,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': TEXT_VALUE},
                             {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]}
        if self.text_endpoint.endswith('/openai/v1'):
            url = self.text_endpoint + '/chat/completions'
        else:
            url = self.text_endpoint + f'/openai/deployments/{self.text_deployment}/chat/completions?api-version=2024-10-21'
        result, record = self._post(url, body, text=True)
        try:
            if result['model'] not in ('gpt-4.1-mini', 'gpt-4.1-mini-2025-04-14'):
                raise ProviderError('unapproved text helper model')
            output = json.loads(result['choices'][0]['message']['content'])
            value = output['text']
            if set(output) != {'text'} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise ValueError()
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            raise ProviderError('text helper returned no valid field value') from None
        return value, {'model': result['model'], **record}
