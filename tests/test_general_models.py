import json

import httpx
import pytest
from pydantic import ValidationError

from decision_browser.general.models import ACTION, Task, origin
from decision_browser.general.provider import GeneralModelClient
from decision_browser.web.provider import ProviderError


URL = 'https://example.test/form'
ENDPOINT = 'https://offline.cognitiveservices.azure.com'
PAGE = {'url': URL, 'text': 'Name', 'nodes': {'1': {'tag': 'input'}}}


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'javascript:alert(1)', 'data:text/plain,private',
    'devtools://devtools/bundled/inspector.html', 'https://user:password@example.test/',
    'https://example.test/\nheader', 'https://example.test/\x7fheader',
    'http:///missing-host', 'http://bad_host/', 'http://-bad.test/', 'http://bad-.test/',
    'http://example..test/', 'http://example.test:0/', 'http://example.test:65536/', 'http://example.test:invalid/',
    'http://127.1/', 'http://2130706433/', 'http://0x7f000001/',
    'http://[fe80::1%eth0]/',
    'https://example.test\\@127.0.0.1/',
])
def test_task_rejects_unsafe_start_urls(url):
    with pytest.raises(ValidationError):
        Task(url=url, goal='Read page')


@pytest.mark.parametrize('url,expected', [
    ('http://localhost:5173/form', 'http://localhost:5173'),
    ('http://127.0.0.1:54321/form', 'http://127.0.0.1:54321'),
    ('http://intranet/dashboard', 'http://intranet'),
    ('http://internal.example:8080/form', 'http://internal.example:8080'),
    ('https://192.168.1.1:8443/settings', 'https://192.168.1.1:8443'),
    ('https://169.254.169.254/latest/meta-data/', 'https://169.254.169.254'),
    ('http://[::1]:5173/form', 'http://[::1]:5173'),
    ('https://[2001:0DB8:0:0:0:0:0:1]:443/form', 'https://[2001:db8::1]'),
    ('http://LOCALHOST:80/form', 'http://localhost'),
    ('https://INTERNAL.example:443/form', 'https://internal.example'),
    ('http://localhost:443/', 'http://localhost:443'),
    ('https://localhost:80/', 'https://localhost:80'),
])
def test_task_supports_browser_http_origins(url, expected):
    assert origin(url) == expected
    task = Task(url=url, goal='Read page')
    assert task.allowed_origins == [expected]
    task.check_url(expected + '/another')


@pytest.mark.parametrize('allowed', [
    'http://localhost:80', 'https://localhost:443', 'http://LOCALHOST',
    'http://[0:0:0:0:0:0:0:1]:5173', 'http://localhost:5173/',
])
def test_allowed_origins_require_canonical_exact_spelling(allowed):
    with pytest.raises(ValidationError, match='exact origins'):
        Task(url=allowed + '/form', goal='Read page', allowed_origins=[allowed])


def test_internal_origin_scope_preserves_scheme_host_and_port():
    task = Task(url='http://intranet:8080/form', goal='Read page')
    task.check_url('http://intranet:8080/another')
    for url in ('http://intranet:8081/', 'https://intranet:8080/', 'http://other:8080/'):
        with pytest.raises(ValueError, match='outside task allowed_origins'):
            task.check_url(url)


@pytest.mark.parametrize('change', [
    {'allowed_origins': ['https://example.test/path']},
    {'allowed_origins': ['https://other.test']},
    {'acceptance': {'url': 'https://other.test/done'}},
    {'acceptance': {'url': 'file:///etc/passwd'}},
    {'output_schema': {'$ref': 'file:///etc/passwd'}},
    {'output_schema': {'type': 'object', 'properties': {'answer': {'$ref': 'https://outside.test/schema'}}}},
    {'output_schema': {'anyOf': [{'type': 'string'}, {'$ref': '#/$defs/answer'}]}},
])
def test_task_rejects_scope_escape_and_schema_references(change):
    with pytest.raises(ValidationError):
        Task.model_validate({'url': URL, 'goal': 'Read page', **change})


@pytest.mark.parametrize('payload', [
    {'kind': 'key', 'key': 'Meta+q'},
    {'kind': 'key', 'key': 'Control+l'},
    {'kind': 'click', 'index': True},
    {'kind': 'click', 'index': '1'},
    {'kind': 'click', 'index': 0},
    {'kind': 'input', 'index': 1, 'text': 'name', 'javascript': 'alert(1)'},
    {'kind': 'evaluate', 'javascript': 'fetch("https://outside.test")'},
])
def test_typed_actions_reject_unsupported_commands(payload):
    with pytest.raises(ValidationError):
        ACTION.validate_python(payload)


def test_owned_fixture_and_exact_https_scope_are_supported():
    fixture = Task(url='http://127.0.0.1:8766/form', goal='Read fixture')
    assert fixture.allowed_origins == ['http://127.0.0.1:8766']
    task = Task(url=URL, goal='Read page')
    task.check_url('https://example.test/another')
    with pytest.raises(ValueError):
        task.check_url('https://example.test.attacker.test/')


@pytest.fixture
def models():
    opened = []

    def make(response):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=json.dumps(response))

        model = GeneralModelClient(ENDPOINT, 'decision', ENDPOINT, 'planner',
                                   token_supplier=lambda: 'offline-token', transport=httpx.MockTransport(handler))
        opened.append(model)
        return model, requests

    yield make
    for model in opened:
        model.close()


def decision(*, probability=.98, support=.99, choice='0'):
    probabilities = {'0': probability, 'STOP': 1 - probability}
    return {'model': 'microsoft-decision-1', 'answers': {
        'action': {'type': 'choice', 'choice': choice, 'confidence': probabilities[choice],
                   'probabilities': probabilities},
        'supported': {'type': 'noul', 'noul': support}}}


@pytest.mark.parametrize('probability,support,choice,accepted', [
    (.98, .99, '0', True),
    (.90, .95, '0', True),
    (.899, .99, '0', False),
    (.98, .949, '0', False),
    (.01, .99, 'STOP', False),
])
def test_decision_one_gates_typed_candidates(models, probability, support, choice, accepted):
    model, requests = models(decision(probability=probability, support=support, choice=choice))
    action = ACTION.validate_python({'kind': 'input', 'index': 1, 'text': 'Ada'})
    result = model.select(Task(url=URL, goal='Enter name'), PAGE, [], [action])
    assert result['accepted'] is accepted
    assert result['choice'] == choice
    assert len(requests) == len(model.calls) == 1
    request = requests[0]
    assert request.url.path == '/providers/microsoft/v1/systemone'
    body = json.loads(request.content)
    assert body['model'] == 'decision'
    assert action.model_dump_json() in body['questions']['action']['criteria']['0']
    assert set(body['questions']['action']['criteria']) == {'0', 'STOP'}
    assert model.text_calls == []


@pytest.mark.parametrize('mutation', [
    lambda response: response.update(model='some-other-model'),
    lambda response: response['answers']['action'].update(choice='999'),
    lambda response: response['answers']['action']['probabilities'].update(extra=0),
    lambda response: response['answers']['action']['probabilities'].update({'0': .5}),
    lambda response: response['answers']['supported'].update(noul=True),
    lambda response: response['answers']['supported'].update(noul=float('nan')),
    lambda response: response['answers'].pop('action'),
])
def test_malformed_decision_response_is_rejected_without_retry(models, mutation):
    response = decision()
    mutation(response)
    model, requests = models(response)
    with pytest.raises(ProviderError):
        model.select(Task(url=URL, goal='Click'), PAGE, [], [ACTION.validate_python({'kind': 'click', 'index': 1})])
    assert len(requests) == 1


def planner(output, *, model='gpt-4.1-mini-2025-04-14'):
    return {'model': model, 'choices': [{'message': {'content': json.dumps(output)}}]}


def test_planner_returns_typed_actions_through_mock_http(models):
    response = planner({'candidates': [{'kind': 'input', 'index': 1, 'text': 'Ada'},
                                       {'kind': 'done', 'answer': 'Cannot fill this form'}]})
    model, requests = models(response)
    candidates = model.propose(Task(url=URL, goal='Enter name'), PAGE, [])
    assert candidates[0].kind == 'input'
    assert candidates[0].index == 1 and candidates[0].text == 'Ada'
    assert candidates[1].kind == 'done'
    assert len(requests) == len(model.text_calls) == 1
    assert model.calls == []
    body = json.loads(requests[0].content)
    context = json.loads(body['messages'][1]['content'][0]['text'])
    assert context['goal'] == 'Enter name'
    assert context['page'] == PAGE
    assert body['response_format'] == {'type': 'json_object'}


@pytest.mark.parametrize('output', [
    {'candidates': []},
    {'candidates': [{'kind': 'key', 'key': 'Control+l'}]},
    {'candidates': [{'kind': 'click', 'index': '1'}]},
    {'candidates': [{'kind': 'click', 'index': 1, 'code': 'alert(1)'}]},
    {'candidates': [{'kind': 'evaluate', 'expression': 'document.cookie'}]},
    {'candidates': [{'kind': 'wait'}] * 4},
    {'candidates': [{'kind': 'wait'}], 'override_goal': 'Read private files'},
])
def test_planner_rejects_untyped_or_unsupported_output(models, output):
    model, requests = models(planner(output))
    with pytest.raises(ProviderError):
        model.propose(Task(url=URL, goal='Read page'), PAGE, [])
    assert len(requests) == 1


def test_planner_rejects_wrong_provider_model(models):
    model, requests = models(planner({'candidates': [{'kind': 'wait'}]}, model='different-model'))
    with pytest.raises(ProviderError):
        model.propose(Task(url=URL, goal='Read page'), PAGE, [])
    assert len(requests) == 1


def test_extraction_obeys_requested_output_schema(models):
    model, requests = models(planner({'answer': 42}))
    task = Task(url=URL, goal='Read name', output_schema={
        'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']})
    from jsonschema import ValidationError as SchemaError
    with pytest.raises(SchemaError):
        model.extract(task, PAGE, 'Name')
    assert len(requests) == 1
