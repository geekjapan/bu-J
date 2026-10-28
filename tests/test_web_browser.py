"""Offline CDP transport and real JavaScript guard checks. No browser is launched."""

import base64
import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import websocket

from decision_browser.web import browser as module
from decision_browser.web.browser import Browser, NotDispatched, StalePage
from decision_browser.web.tasks import get_task


class CDPSocket:
    def __init__(self, task):
        self.task = task
        self.sent = []
        self.closed = False
        self.events = []
        self.fail = None
        self.changed = False
        self.covered = False
        self.select_interrupted = False
        self.target_timeout = False
        self.metadata = {
            '1': {'tag': 'input', 'input_type': 'search', 'href': '',
                  'form_action': 'https://en.wikipedia.org/w/index.php', 'form_method': 'get',
                  'context': 'Search Wikipedia',
                  'attributes': {'id': 'searchInput', 'name': 'search', 'role': 'searchbox',
                                 'aria-label': 'Search Wikipedia', 'data-place': ''}}
        }
        self.action = {'id': 'e1', 'kind': 'fill', 'node': 1, 'role': 'searchbox',
                       'label': 'Search Wikipedia', 'value': '',
                       'rect': {'x': 1, 'y': 2, 'w': 100, 'h': 30}}
        self.page = {'url': task.url, 'title': 'Wikipedia', 'text': 'Search Wikipedia',
                     'actions': [self.action, {'id': 'e2', 'kind': 'click', 'node': 9,
                                              'label': 'Log in', 'role': 'link'}],
                     'marker': ['marker'], 'page_key': ['page'], 'guards': {'1': ['guard']},
                     'scroll': {'y': 0, 'height': 1000}, 'metadata': self.metadata}

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self):
        if self.events:
            return json.dumps(self.events.pop(0))
        request = self.sent[-1]
        method = request['method']
        if self.fail == method:
            raise websocket.WebSocketTimeoutException('injected timeout')
        result = {}
        if method == 'Browser.getVersion':
            result = {'product': 'Chrome/test'}
        elif method == 'Target.createTarget':
            result = {'targetId': 'owned-target'}
        elif method == 'Target.attachToTarget':
            result = {'sessionId': 'owned-session'}
        elif method == 'Page.captureScreenshot':
            result = {'data': base64.b64encode(b'\x89PNG\r\n\x1a\n').decode()}
        elif method == 'Runtime.evaluate':
            expression = request['params']['expression']
            if expression == '({ready:document.readyState,url:location.href})':
                value = {'ready': 'complete', 'url': self.page['url']}
            elif expression == module.METADATA:
                value = {'url': self.page['url'], 'metadata': self.metadata}
            elif expression == module.MARKER:
                value = ['changed'] if self.changed else self.page['marker']
            elif 'c.pageKey(),c.guard' in expression:
                value = [['changed'], ['guard']] if self.changed else [['page'], ['guard']]
            elif 'const state=' in expression:
                value = self.page
            elif '(action => {' in expression:
                if self.target_timeout:
                    raise websocket.WebSocketTimeoutException('injected target timeout')
                if self.select_interrupted:
                    return json.dumps({'id': request['id'], 'result': {'exceptionDetails': {}}})
                value = None if self.covered else {'x': 51, 'y': 17}
            else:
                value = None
            result = {'result': {'value': copy.deepcopy(value)}}
        return json.dumps({'id': request['id'], 'result': result})

    def close(self):
        self.closed = True


@pytest.fixture
def connection(monkeypatch):
    task = get_task('wikipedia')
    socket = CDPSocket(task)
    connections = []

    def connect(endpoint, **kwargs):
        connections.append((endpoint, kwargs))
        return socket

    monkeypatch.setattr(module.websocket, 'create_connection', connect)
    return task, socket, connections


def open_browser(connection, **kwargs):
    task, socket, _ = connection
    return Browser(task, 'ws://127.0.0.1:9222/devtools/browser/test', **kwargs), socket


@pytest.mark.parametrize('endpoint', [
    'ws://example.com:9222/devtools/browser/x', 'http://127.0.0.1:9222',
    'ws://127.0.0.1/devtools/browser/x', 'ws://user:secret@127.0.0.1:9222/x',
    'ws://127.0.0.1:9222/x#fragment', 'ws://127.0.0.1.example.com:9222/x',
])
def test_rejects_unscoped_endpoints_before_connecting(connection, endpoint):
    task, _, connections = connection
    with pytest.raises(ValueError):
        Browser(task, endpoint)
    assert connections == []


@pytest.mark.parametrize('host', ['127.0.0.1', '[::1]', 'localhost'])
def test_owns_fresh_tab_and_closes_only_its_target_and_socket(connection, host):
    task, socket, connections = connection
    socket.events.append({'method': 'Target.targetCreated', 'params': {}})
    browser = Browser(task, f'ws://{host}:9222/devtools/browser/test')
    assert browser.browser_version == 'Chrome/test'
    assert browser.target == 'owned-target'
    assert browser.session == 'owned-session'
    assert browser.cdp_counts['Target.createTarget'] == 1
    activation = next(r for r in socket.sent if r['method'] == 'Target.activateTarget')
    assert activation['params'] == {'targetId': 'owned-target'}
    assert 'sessionId' not in activation
    assert socket.sent.index(activation) < next(i for i,r in enumerate(socket.sent) if r['method']=='Page.navigate')
    assert browser.cdp_counts['Runtime.evaluate'] >= 2
    assert connections[0][1]['suppress_origin'] is True
    viewport = next(r for r in socket.sent if r['method'] == 'Emulation.setDeviceMetricsOverride')
    assert viewport['params']['width'] == 1120
    assert viewport['params']['height'] == 780
    assert viewport['sessionId'] == 'owned-session'
    assert browser.observe()['actions'][-1]['label'] == 'Log in'
    assert isinstance(browser.observe()['fingerprint'], str)
    assert browser.screenshot() == b'\x89PNG\r\n\x1a\n'
    browser.close()
    browser.close()
    close = [r for r in socket.sent if r['method'] == 'Target.closeTarget']
    assert len(close) == 1
    assert close[0]['params'] == {'targetId': 'owned-target'}
    assert 'sessionId' not in close[0]
    assert socket.closed
    assert not any(r['method'] == 'Browser.close' for r in socket.sent)


def test_constructor_failure_closes_created_target_and_socket(connection):
    task, socket, _ = connection
    socket.fail = 'Emulation.setDeviceMetricsOverride'
    with pytest.raises(RuntimeError):
        Browser(task, 'ws://127.0.0.1:9222/devtools/browser/test')
    assert socket.closed
    assert sum(r['method'] == 'Target.closeTarget' for r in socket.sent) == 1


def test_fill_uses_observed_node_and_dispatches_once(connection):
    browser, socket = open_browser(connection)
    page = browser.observe()
    assert browser.act(page['actions'][0], page, 'Tokyo') == {'executed': 'e1'}
    inserts = [r for r in socket.sent if r['method'] == 'Input.insertText']
    assert len(inserts) == 1
    assert inserts[0]['params']['text'] == 'Tokyo'
    assert [r['params']['type'] for r in socket.sent if r['method'] == 'Input.dispatchMouseEvent'] == [
        'mousePressed', 'mouseReleased']
    browser.close()


@pytest.mark.parametrize('enforce_policy', [True, False])
def test_rechecks_policy_metadata_before_input_even_for_baseline(connection, enforce_policy):
    browser, socket = open_browser(connection, enforce_policy=enforce_policy)
    page = browser.observe()
    socket.metadata['1']['input_type'] = 'password'
    socket.metadata['1']['context'] = 'Log in password'
    with pytest.raises(ValueError):
        browser.act(page['actions'][0], page, 'secret')
    assert not any(r['method'].startswith('Input.') for r in socket.sent)
    browser.close()


def test_rechecks_url_scope_before_input(connection):
    browser, socket = open_browser(connection)
    page = browser.observe()
    socket.page['url'] = 'https://evil.example/'
    with pytest.raises(ValueError):
        browser.act(page['actions'][0], page, 'Tokyo')
    assert not any(r['method'].startswith('Input.') for r in socket.sent)
    browser.close()


def test_stale_or_unobserved_actions_do_not_dispatch(connection):
    browser, socket = open_browser(connection)
    page = browser.observe()
    socket.changed = True
    with pytest.raises(NotDispatched):
        browser.act(page['actions'][0], page, 'Tokyo')
    with pytest.raises(ValueError):
        browser.act({**page['actions'][0], 'node': 999}, page, 'Tokyo')
    assert not any(r['method'].startswith('Input.') for r in socket.sent)
    browser.close()


def test_input_timeout_is_uncertain_and_never_replayed(connection):
    browser, socket = open_browser(connection)
    page = browser.observe()
    socket.fail = 'Input.dispatchMouseEvent'
    with pytest.raises(RuntimeError):
        browser.act(page['actions'][0], page, 'Tokyo')
    assert sum(r['method'] == 'Input.dispatchMouseEvent' for r in socket.sent) == 1
    assert not any(r['method'] == 'Input.insertText' for r in socket.sent)
    browser.close()


def test_covered_target_is_stale_before_input(connection):
    browser, socket = open_browser(connection)
    page = browser.observe()
    socket.covered = True
    with pytest.raises(NotDispatched):
        browser.act(page['actions'][0], page, 'Tokyo')
    assert not any(r['method'].startswith('Input.') for r in socket.sent)
    browser.close()


@pytest.mark.parametrize("stage", ["fresh", "metadata", "target"])
def test_pre_input_stale_evaluation_proves_not_dispatched(connection, monkeypatch, stage):
    browser, socket = open_browser(connection)
    page = browser.observe()
    original_evaluate = browser.evaluate
    original_call = browser.call

    def evaluate(expression):
        if (stage == "fresh" and expression == module.MARKER) or (stage == "metadata" and expression == module.METADATA):
            raise StalePage("document changed")
        return original_evaluate(expression)

    def call(method, **params):
        if stage == "target" and method == "Runtime.evaluate" and "(action => {" in params.get("expression", ""):
            return {"exceptionDetails": {"text": "document changed"}}
        return original_call(method, **params)

    monkeypatch.setattr(browser, "evaluate", evaluate)
    monkeypatch.setattr(browser, "call", call)
    with pytest.raises(NotDispatched):
        browser.act(page["actions"][0], page, "Tokyo")
    assert not any(r["method"].startswith("Input.") for r in socket.sent)
    browser.close()


def test_input_exception_is_never_reclassified_as_not_dispatched(connection, monkeypatch):
    browser, socket = open_browser(connection)
    page = browser.observe()
    original_call = browser.call

    def call(method, **params):
        if method == "Input.dispatchMouseEvent":
            raise StalePage("input response interrupted")
        return original_call(method, **params)

    monkeypatch.setattr(browser, "call", call)
    with pytest.raises(StalePage) as error:
        browser.act(page["actions"][0], page, "Tokyo")
    assert not isinstance(error.value, NotDispatched)
    browser.close()


@pytest.mark.parametrize("failure", ["exception", "timeout", "not_confirmed"])
def test_native_select_evaluation_interruption_is_not_retryable(connection, monkeypatch, failure):
    browser, socket = open_browser(connection)
    action = {'id': 'e1', 'node': 1, 'kind': 'select', 'value': '2', 'label': 'Guests 2'}
    page = {**browser.observe(), 'actions': [action]}
    monkeypatch.setattr(module, 'allowed_action', lambda *args: True)
    socket.select_interrupted = failure == "exception"
    socket.target_timeout = failure == "timeout"
    socket.covered = failure == "not_confirmed"
    before = len(socket.sent)
    with pytest.raises(RuntimeError):
        browser.act(action, page)
    target_requests = [r for r in socket.sent[before:] if '(action => {' in r['params'].get('expression', '')]
    assert len(target_requests) == 1
    assert not any(r['method'].startswith('Input.') for r in socket.sent)
    browser.close()


def test_pinned_snapshot_and_license_hashes():
    upstream = Path(module.__file__).with_name('upstream')
    provenance = json.loads((upstream / 'UPSTREAM.json').read_text())
    assert hashlib.sha256((upstream / 'snapshot.js').read_bytes()).hexdigest() == provenance['original_sha256']['jev_ultrafast/snapshot.js']
    assert hashlib.sha256((upstream / 'LICENSE').read_bytes()).hexdigest() == provenance['original_sha256']['LICENSE']
    assert len(provenance['original_sha256']['jev_ultrafast/browser.py']) == 64


def test_real_action_javascript_guards_hit_test_disabled_readonly_and_select(connection):
    if shutil.which('node') is None:
        pytest.fail('Node is required for the offline DOM guard check')
    browser, socket = open_browser(connection)
    page = browser.observe()
    browser.act(page['actions'][0], page, 'Tokyo')
    expression = next(r['params']['expression'] for r in socket.sent
                      if '(action => {' in r['params'].get('expression', ''))
    # Execute the shipped target guard rather than a Python copy of its conditions.
    script = r'''
const assert=require('node:assert/strict');
const expression=JSON.parse(process.argv[1]);
const source=expression.slice(0,expression.lastIndexOf('})(')+2);
let disabled=false,readonly=false,covered=false,hidden=false,connected=true;
let x=20,tag='INPUT', events=[];
const element={get isConnected(){return connected}, get readOnly(){return readonly},
  get tagName(){return tag}, value:'old', options:[{value:'2',disabled:false,closest:()=>null}],
  matches:()=>disabled,closest:()=>null,getAttribute:()=>null,
  checkVisibility:()=>!hidden,getBoundingClientRect:()=>({x,y:10,width:20,height:20}),
  contains:e=>e===element, dispatchEvent:e=>events.push(e.type)};
global.window={__jevFast:{nodes:new Map([[1,element]])}};
global.document={elementFromPoint:()=>covered?{}:element};
global.innerWidth=1120;global.innerHeight=780;
const act=eval(source);
assert.deepEqual(act({node:1,kind:'fill'}),{x:30,y:20});
disabled=true;assert.equal(act({node:1,kind:'fill'}),null);disabled=false;
readonly=true;assert.equal(act({node:1,kind:'fill'}),null);readonly=false;
covered=true;assert.equal(act({node:1,kind:'fill'}),null);covered=false;
hidden=true;assert.equal(act({node:1,kind:'fill'}),null);hidden=false;
connected=false;assert.equal(act({node:1,kind:'fill'}),null);connected=true;
x=-50;assert.equal(act({node:1,kind:'fill'}),null);x=20;
assert.equal(act({node:1,kind:'select',value:'2'}),null);
tag='SELECT';assert.equal(act({node:1,kind:'select',value:'absent'}),null);
assert.deepEqual(act({node:1,kind:'select',value:'2'}),{x:30,y:20});
assert.equal(element.value,'2');assert.deepEqual(events,['input','change']);
'''
    result = subprocess.run(['node', '-e', script, json.dumps(expression)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    browser.close()


def test_metadata_reads_form_overrides_and_stable_node_keys():
    if shutil.which('node') is None:
        pytest.fail('Node is required for the offline metadata check')
    script = r'''
const assert=require('node:assert/strict');
const expression=JSON.parse(process.argv[1]);
const attributes={id:'search',name:'search',role:'searchbox','aria-label':'Search','data-place':''};
const element={isConnected:true,tagName:'INPUT',type:'search',name:'search',
  form:{action:'https://en.wikipedia.org/w/index.php',method:'get'},
  formAction:'https://evil.example/submit',formMethod:'post',
  getAttribute:key=>attributes[key] || null,
  hasAttribute:key=>['formaction','formmethod'].includes(key),
  closest:selector=>selector==='a[href]'?null:{innerText:'Search Wikipedia'},
  parentElement:{innerText:'Search Wikipedia'}};
global.window={__jevFast:{nodes:new Map([[42,element]])}};
global.location={href:'https://en.wikipedia.org/wiki/Main_Page'};
let result=eval(expression);
assert.equal(result.url,location.href);
assert.deepEqual(Object.keys(result.metadata),['42']);
assert.equal(result.metadata['42'].form_action,'https://evil.example/submit');
assert.equal(result.metadata['42'].form_method,'post');
assert.equal(result.metadata['42'].input_type,'search');
assert.equal(result.metadata['42'].context,'Search Wikipedia');
assert.deepEqual(result.metadata['42'].attributes,attributes);
element.hasAttribute=()=>false;
result=eval(expression);
assert.equal(result.metadata['42'].form_action,'https://en.wikipedia.org/w/index.php');
assert.equal(result.metadata['42'].form_method,'get');
element.isConnected=false;
assert.deepEqual(eval(expression).metadata,{});
'''
    result = subprocess.run(['node', '-e', script, json.dumps(module.METADATA)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
