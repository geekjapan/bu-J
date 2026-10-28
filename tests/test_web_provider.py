import json
import subprocess

import httpx
import pytest

from decision_browser.web.provider import ModelClient, ProviderError, _azure_token, field_context

ENDPOINT = 'https://benchmark.cognitiveservices.azure.com'
TOKEN = 'offline-secret-token'
PAGE = {'url': 'https://example.test/', 'title': 'Search', 'text': 'Page content', 'actions': [
    {'id': 'fill:1', 'kind': 'fill', 'node': 'n1', 'label': 'Query', 'role': 'textbox', 'value': ''},
    {'id': 'click:2', 'kind': 'click', 'node': 'n2', 'label': 'Search'},
    {'id': 'click:3', 'kind': 'click', 'node': 'n3', 'label': 'Other'},
]}


def answer(choice, probabilities):
    return {'type': 'choice', 'choice': choice, 'confidence': probabilities[choice], 'probabilities': probabilities}


def response():
    return {'model': 'microsoft-decision-1', 'answers': {
        'operation': answer('CLICK', {'TYPE_TEXT': .01, 'CLICK': .97, 'DONE': .01, 'BLOCKED': .01}),
        'click_target': answer('2', {'2': .96, '3': .04}),
        'type_text_target': {'malformed': 'unused'},
        'state_supported': {'type': 'noul', 'noul': .99}},
        'usage': {'input_tokens': 10, 'output_tokens': 5}}


def client(handler, **kwargs):
    return ModelClient(ENDPOINT, 'decision-deployment', ENDPOINT, 'text-deployment',
                       token_supplier=lambda: TOKEN, transport=httpx.MockTransport(handler), **kwargs)


def test_dynamic_single_request_and_selected_head():
    requests = []
    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body['model'] == 'decision-deployment'
        assert set(body['questions']) == {'operation', 'click_target', 'type_text_target', 'state_supported'}
        assert body['questions']['state_supported']['type'] == 'noul'
        assert body['state']['goal'] == 'Search'
        assert isinstance(body['state']['progress']['next_step'], str)
        assert isinstance(body['questions']['operation']['instructions'], str)
        assert set(body['questions']['state_supported']['criteria']) == {'true', 'false'}
        assert 'all future steps' in body['questions']['state_supported']['instructions']
        assert set(body['questions']['click_target']['criteria']) == {'2', '3'}
        assert request.headers['Authorization'] == f'Bearer {TOKEN}'
        return httpx.Response(200, json=response(), headers={'x-request-id': 'request-123'})
    model = client(handler)
    result = model.choose(PAGE, 'Search', [])
    assert result['accepted'] and result['choice'] == 'click:2' and result['operation'] == 'CLICK'
    assert len(requests) == len(model.calls) == 1 and model.total_tokens == 15
    assert model.calls[0]['request_id'] == 'request-123'
    assert len(model.calls[0]['request_digest']) == 64
    assert TOKEN not in json.dumps(model.calls)
    model.close()
    assert model._token is None


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(model='decision-deployment'),
    lambda r: r['answers']['click_target']['probabilities'].update(extra=0),
    lambda r: r['answers']['click_target']['probabilities'].update({'2': float('nan')}),
    lambda r: r['answers']['click_target']['probabilities'].update({'2': .8}),
    lambda r: r['answers']['click_target'].update(choice='3'),
    lambda r: r['answers'].pop('click_target'),
    lambda r: r['answers']['state_supported'].update(noul=True),
])
def test_malformed_response_rejected(mutation):
    data = response()
    mutation(data)
    # Raw encoding permits NaN specifically to test the response boundary.
    model = client(lambda _: httpx.Response(200, content=json.dumps(data)))
    with pytest.raises(ProviderError):
        model.choose(PAGE, 'Search', [])
    assert len(model.calls) == 1
    model.close()


@pytest.mark.parametrize('head', ['operation', 'click_target', 'support'])
def test_fixed_gate_abstains(head):
    data = response()
    if head == 'support':
        data['answers']['state_supported']['noul'] = .949
    elif head == 'operation':
        data['answers']['operation'] = answer('CLICK', {'TYPE_TEXT': .10, 'CLICK': .89, 'DONE': .01, 'BLOCKED': 0})
    else:
        data['answers']['click_target'] = answer('2', {'2': .899, '3': .101})
    model = client(lambda _: httpx.Response(200, json=data))
    assert not model.choose(PAGE, 'Search', [])['accepted']
    model.close()


def test_done_does_not_validate_unused_heads():
    data = response()
    data['answers']['operation'] = answer('DONE', {'TYPE_TEXT': 0, 'CLICK': 0, 'DONE': 1, 'BLOCKED': 0})
    data['answers']['click_target'] = None
    model = client(lambda _: httpx.Response(200, json=data))
    result = model.choose(PAGE, 'Done', [])
    assert result['accepted'] and result['choice'] == 'DONE' and result['target'] is None
    model.close()


@pytest.mark.parametrize('head', ['operation', 'click_target'])
def test_tied_top_is_valid_provider_data_but_fails_bu_j_margin(head):
    data = response()
    if head == 'operation':
        data['answers'][head] = answer('CLICK', {'TYPE_TEXT': .5, 'CLICK': .5, 'DONE': 0, 'BLOCKED': 0})
    else:
        data['answers'][head] = answer('2', {'2': .5, '3': .5})
    model = client(lambda _: httpx.Response(200, json=data))
    result = model.choose(PAGE, 'Search', [])
    assert result['choice'] == 'click:2'
    assert result['operation'] == 'CLICK'
    assert result['accepted'] is False
    model.close()


def text_response(content='{"text":"generated value"}', model='gpt-4.1-mini-2025-04-14'):
    return {'model': model, 'choices': [{'message': {'content': content}}],
            'usage': {'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9,
                      'completion_tokens_details': {'reasoning_tokens': 0}}}


@pytest.mark.parametrize('base', [ENDPOINT, ENDPOINT + '/openai/v1'])
def test_real_text_request_shape_and_context(base):
    def handler(request):
        body = json.loads(request.content)
        assert body['max_tokens'] == 256 and body['temperature'] == 0
        assert body['response_format'] == {'type': 'json_object'}
        assert body['model'] == 'text-deployment'
        assert request.url.path.endswith('/chat/completions')
        if '/deployments/' in request.url.path:
            assert request.url.params['api-version'] == '2024-10-21'
        return httpx.Response(200, json=text_response())
    model = ModelClient(ENDPOINT, 'decision', base, 'text-deployment', token_supplier=lambda: TOKEN,
                        transport=httpx.MockTransport(handler))
    context = field_context('Original goal', PAGE['actions'][0], PAGE, [])
    assert context == model.field_context('Original goal', PAGE['actions'][0], PAGE, [])
    value, metadata = model.field_text(context)
    assert value == 'generated value' and value != context['goal']
    assert metadata['http_status'] == 200 and model.total_tokens == 9
    model.close()


@pytest.mark.parametrize('content,model_name', [
    ('{"text":null}', 'gpt-4.1-mini'), ('{"text":""}', 'gpt-4.1-mini'),
    ('{"text":"x","extra":1}', 'gpt-4.1-mini'), ('not JSON', 'gpt-4.1-mini'),
    (json.dumps({'text': 'x' * 2001}), 'gpt-4.1-mini'), ('{"text":"x"}', 'unapproved'),
])
def test_text_validation(content, model_name):
    model = client(lambda _: httpx.Response(200, json=text_response(content, model_name)))
    with pytest.raises(ProviderError):
        model.field_text({'goal': 'must not be copied'})
    model.close()


@pytest.mark.parametrize('status', [302, 429, 503])
def test_no_redirect_or_retry_and_sanitized_error(status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=TOKEN, headers={'Location': 'https://unapproved.test/', 'x-request-id': TOKEN})
    model = client(handler)
    with pytest.raises(ProviderError):
        model.choose(PAGE, 'Search', [])
    assert len(requests) == 1
    assert TOKEN not in json.dumps(model.calls)
    model.close()


def test_caps_count_failed_http_but_not_oversized_input():
    model = client(lambda _: httpx.Response(503), max_calls=1, max_text_calls=1)
    with pytest.raises(ProviderError, match='request byte'):
        model.choose(PAGE, 'x' * 1_000_001, [])
    assert not model.calls
    with pytest.raises(ProviderError, match='HTTP status'):
        model.choose(PAGE, 'Search', [])
    with pytest.raises(ProviderError, match='call budget'):
        model.choose(PAGE, 'Search', [])
    with pytest.raises(ProviderError):
        model.field_text({})
    with pytest.raises(ProviderError, match='call budget'):
        model.field_text({})
    assert len(model.calls) == len(model.text_calls) == 1
    model.close()


def test_response_limit_and_transport_failure_are_single_attempt():
    for handler in [lambda _: httpx.Response(200, content=b'x' * 1_000_001),
                    lambda _: (_ for _ in ()).throw(httpx.ConnectError(TOKEN))]:
        model = client(handler)
        with pytest.raises(ProviderError) as error:
            model.choose(PAGE, 'Search', [])
        assert TOKEN not in str(error.value) and len(model.calls) == 1
        model.close()


@pytest.mark.parametrize('endpoint', ['http://x.cognitiveservices.azure.com', 'https://evil.test',
    ENDPOINT + '?', ENDPOINT + '#', ENDPOINT + '/path', 'https://user@benchmark.cognitiveservices.azure.com'])
def test_endpoint_validation(endpoint):
    with pytest.raises(ValueError):
        ModelClient(endpoint, 'decision', ENDPOINT, 'text', token_supplier=lambda: TOKEN)


def test_azure_cli_exact_command_and_no_saved_credential(monkeypatch):
    commands = []
    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, TOKEN + '\n', '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert _azure_token() == TOKEN
    assert commands[0][0] == ['az', 'account', 'get-access-token', '--resource',
                              'https://cognitiveservices.azure.com/', '--query', 'accessToken', '--output', 'tsv']
    assert commands[0][1] == {'capture_output': True, 'text': True, 'check': True, 'timeout': 25}

@pytest.mark.parametrize('choose_none', [False, True])
def test_singleton_target_is_a_real_choice_with_safe_abstention(choose_none):
    def handler(request):
        body = json.loads(request.content)
        assert set(body['questions']['type_text_target']['criteria']) == {'1', '__none__'}
        data=response()
        data['answers']['operation']=answer('TYPE_TEXT', {'TYPE_TEXT':.98,'CLICK':.01,'DONE':.005,'BLOCKED':.005})
        probs={'1':.01 if choose_none else .99,'__none__':.99 if choose_none else .01}
        data['answers']['type_text_target']=answer('__none__' if choose_none else '1',probs)
        return httpx.Response(200,json=data)
    model=client(handler)
    result=model.choose(PAGE,'Search',[])
    assert result['accepted'] is (not choose_none)
    assert result['choice'] == ('BLOCKED' if choose_none else 'fill:1')
    model.close()
