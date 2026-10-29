from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from decision_browser.general.browser import GeneralBrowser, NotDispatched
from decision_browser.general.models import ACTION, Task


URL = 'http://127.0.0.1:8766/form'
TAB = 'FULL-TARGET-123456789'


class Commands:
    def __init__(self, fake, domain=''):
        self.fake, self.domain = fake, domain

    def __getattr__(self, name):
        if not self.domain:
            return Commands(self.fake, name)

        async def send(params, session_id):
            self.fake.calls.append((self.domain, name, params, session_id))
            if name == self.fake.fail:
                raise RuntimeError('transport disconnected after send')
            if name == 'getContentQuads':
                return {'quads': [[100, 200, 200, 200, 200, 240, 100, 240]]}
            if name == 'getNodeForLocation':
                return {'backendNodeId': 42}
            if name == 'resolveNode':
                return {'object': {'objectId': 'captured-object'}}
            if name == 'callFunctionOn':
                function = params['functionDeclaration']
                if 'Array.from(this.options)' in function:
                    return {'result': {'value': True}}
                if 'this.value = value' in function:
                    return {'result': {'value': params['arguments'][0]['value']}}
                guard = self.fake.guard.copy()
                guard['attrs'] = dict(guard['attrs'])
                args = params.get('arguments', [])
                if args and args[0]['value'] is True:
                    guard['attrs']['value'] = guard.get('value', '')
                if len(args) > 1 and args[1]['value'] is True:
                    if guard.get('checked'):
                        guard['attrs']['checked'] = 'true'
                    else:
                        guard['attrs'].pop('checked', None)
                return {'result': {'value': guard}}
            if name == 'evaluate':
                if params.get('expression') == '({url: location.href, title: document.title})':
                    return {'result': {'value': self.fake.metadata()}}
                return {'result': {'objectId': 'focused-object'}}
            if name == 'getNavigationHistory':
                return {'currentIndex': 1, 'entries': [
                    {'id': 1, 'url': URL}, {'id': 2, 'url': URL + '?next'},
                    {'id': 3, 'url': URL + '?last'},
                ]}
            if name == 'createTarget':
                return {'targetId': 'NEW-FULL-TARGET'}
            return {}
        return send


@pytest.fixture
def browser():
    instance = GeneralBrowser('http://owned-cdp:9222', Task(url=URL, goal='fill form'), {'file': '/tmp/bu-j-uploads/uuid/file.txt'})
    fake = SimpleNamespace(calls=[], fail=None, guard={
        'connected': True, 'tag': 'input', 'attrs': {'type': 'text'}, 'url': URL,
        'disabled': False, 'editable': False, 'x': 50, 'y': 30,
        'visible': True, 'hit': True, 'focused': True,
    })
    cdp = SimpleNamespace(session_id='captured-session', cdp_client=SimpleNamespace(send=Commands(fake)))
    node = SimpleNamespace(backend_node_id=42, session_id='captured-session', frame_id='frame', target_id=TAB,
                           node_name='INPUT', attributes={'type': 'text'})
    dom = SimpleNamespace(selector_map={1: node}, llm_representation=lambda: '[1]<input type=text> form text')
    raw = SimpleNamespace(url=URL, title='Form', tabs=[SimpleNamespace(target_id=TAB, url=URL, title='Form')], dom_state=dom, state_error=None)
    instance.session = SimpleNamespace(
        agent_focus_target_id=TAB, get_current_page_url=AsyncMock(return_value=URL), get_all_frames=AsyncMock(return_value=({'frame': {'url': URL}}, {})),
        get_browser_state_summary=AsyncMock(return_value=raw), get_or_create_cdp_session=AsyncMock(return_value=cdp),
        cdp_client_for_node=AsyncMock(return_value=cdp), take_screenshot=AsyncMock(return_value=b'png'), stop=AsyncMock(),
    )
    instance.fake, instance.node, instance.raw, instance.cdp = fake, node, raw, cdp
    fake.metadata = lambda: {'url': instance.raw.url, 'title': instance.raw.title}
    return instance


async def execute(browser, payload):
    observed = await browser.observe()
    return await browser.execute(ACTION.validate_python(payload), observed)


def mutations(browser):
    return [(d, m, p) for d, m, p, _ in browser.fake.calls
            if d in ('Input', 'Target') or m in ('focus', 'setFileInputFiles', 'navigate', 'reload', 'navigateToHistoryEntry')]


async def test_observation_retains_full_ids_and_raw_summary(browser):
    view = await browser.observe()
    assert view['tabs'][0]['tab_id'] == TAB
    assert view['nodes']['1']['backend_node_id'] == 42
    assert browser.observation is browser.raw
    assert view['dom_truncated'] is False
    browser.session.get_browser_state_summary.assert_awaited_once_with(include_screenshot=False, cached=False)
    assert await browser.screenshot() == 'cG5n'


async def test_observation_explicitly_reports_truncation(browser):
    browser.raw.dom_state.llm_representation = lambda: 'z' * 100001
    view = await browser.observe()
    assert view['dom_truncated'] and view['dom_characters'] == 100001
    assert len(view['dom']) == 100000


async def test_foreign_frame_prevents_observation(browser):
    browser.session.get_all_frames.return_value[0]['foreign'] = {'url': 'https://other.example/secret'}
    with pytest.raises(NotDispatched):
        await browser.observe()
    browser.session.get_browser_state_summary.assert_not_awaited()


async def test_index_remains_bound_to_captured_backend_node(browser):
    view = await browser.observe()
    browser.raw.dom_state.selector_map[1] = SimpleNamespace(backend_node_id=999)
    await browser.execute(ACTION.validate_python({'kind': 'click', 'index': 1}), view)
    resolve = next(p for _, method, p, _ in browser.fake.calls if method == 'resolveNode')
    assert resolve == {'backendNodeId': 42}


@pytest.mark.parametrize('changed', [
    {'connected': False}, {'tag': 'textarea'}, {'attrs': {'type': 'password'}},
    {'url': 'https://other.example/'}, {'disabled': True}, {'hit': False},
])
async def test_changed_or_out_of_scope_node_sends_no_input(browser, changed):
    observed = await browser.observe()
    browser.fake.guard.update(changed)
    with pytest.raises(NotDispatched):
        await browser.execute(ACTION.validate_python({'kind': 'click', 'index': 1}), observed)
    assert not mutations(browser)


async def test_routing_fallback_to_another_session_is_rejected(browser):
    browser.cdp.session_id = 'replacement-session'
    with pytest.raises(NotDispatched, match='session changed'):
        await execute(browser, {'kind': 'input', 'index': 1, 'text': 'value'})
    assert not mutations(browser)


@pytest.mark.parametrize('failure', ['dispatchMouseEvent', 'focus', 'dispatchKeyEvent', 'insertText'])
async def test_input_errors_propagate_without_retry(browser, failure):
    browser.fake.fail = failure
    kind = 'click' if failure == 'dispatchMouseEvent' else 'input'
    payload = {'kind': kind, 'index': 1}
    if kind == 'input':
        payload['text'] = 'value'
    with pytest.raises(RuntimeError, match='after send'):
        await execute(browser, payload)
    assert sum(m == failure for _, m, _, _ in browser.fake.calls) == 1
    assert browser.fake.calls[-1][1] == failure


async def test_text_input_uses_native_focus_select_all_and_insert_once(browser):
    await execute(browser, {'kind': 'input', 'index': 1, 'text': 'hello'})
    assert [m for _, m, _ in mutations(browser)] == ['focus', 'dispatchKeyEvent', 'dispatchKeyEvent', 'insertText']
    assert mutations(browser)[-1][2] == {'text': 'hello'}


async def test_password_input_is_rejected(browser):
    browser.node.attributes = {'type': 'password'}
    browser.fake.guard['attrs'] = {'type': 'password'}
    with pytest.raises(NotDispatched):
        await execute(browser, {'kind': 'input', 'index': 1, 'text': 'secret'})
    assert not mutations(browser)


@pytest.mark.parametrize('payload', [
    {'kind': 'navigate', 'url': 'https://other.example/'},
    {'kind': 'switch_tab', 'tab_id': '6789'},
    {'kind': 'close_tab', 'tab_id': 'unobserved-target'},
    {'kind': 'upload', 'index': 1, 'file': 'unknown'},
])
async def test_invalid_actions_never_mutate(browser, payload):
    with pytest.raises(NotDispatched):
        await execute(browser, payload)
    assert not mutations(browser)


async def test_tampered_or_evicted_observation_never_mutates(browser):
    observed = await browser.observe()
    observed['nodes']['1']['backend_node_id'] = 999
    with pytest.raises(NotDispatched, match='Unknown observation'):
        await browser.execute(ACTION.validate_python({'kind': 'click', 'index': 1}), observed)
    assert not mutations(browser)


@pytest.mark.parametrize('payload, method', [
    ({'kind': 'navigate', 'url': URL}, 'navigate'),
    ({'kind': 'navigate', 'url': URL, 'new_tab': True}, 'createTarget'),
    ({'kind': 'reload'}, 'reload'),
    ({'kind': 'back'}, 'navigateToHistoryEntry'),
    ({'kind': 'forward'}, 'navigateToHistoryEntry'),
    ({'kind': 'scroll', 'dy': 100}, 'dispatchMouseEvent'),
    ({'kind': 'switch_tab', 'tab_id': TAB}, 'activateTarget'),
    ({'kind': 'close_tab', 'tab_id': TAB}, 'closeTarget'),
])
async def test_nonindexed_actions_send_expected_command_once(browser, payload, method):
    result = await execute(browser, payload)
    assert result['returned'] is True
    assert [m for _, m, _ in mutations(browser)] == [method]


async def test_select_and_upload_use_captured_node(browser):
    browser.node.node_name = 'SELECT'
    browser.fake.guard['tag'] = 'select'
    await execute(browser, {'kind': 'select', 'index': 1, 'value': 'red'})
    selected = [p for _, m, p, _ in browser.fake.calls if m == 'callFunctionOn' and 'this.value = value' in p['functionDeclaration']]
    assert len(selected) == 1 and selected[0]['arguments'] == [{'value': 'red'}]
    browser.node.node_name = 'INPUT'
    browser.node.attributes = {'type': 'file'}
    browser.fake.guard.update(tag='input', attrs={'type': 'file'})
    await execute(browser, {'kind': 'upload', 'index': 1, 'file': 'file'})
    assert mutations(browser)[-1] == ('DOM', 'setFileInputFiles', {'backendNodeId': 42, 'files': ['/tmp/bu-j-uploads/uuid/file.txt']})


async def test_key_rejects_unvalidated_iframe_focus(browser):
    browser.fake.guard['tag'] = 'iframe'
    with pytest.raises(NotDispatched, match='Focused frame'):
        await execute(browser, {'kind': 'key', 'key': 'Enter'})
    assert not mutations(browser)


async def test_node_bounds_are_explicit_and_unexposed_indices_rejected(browser):
    browser.raw.dom_state.selector_map = {i: browser.node for i in range(1, 2002)}
    view = await browser.observe()
    assert view['nodes_truncated'] and view['nodes_total'] == 2001
    assert len(view['nodes']) == 2000
    with pytest.raises(NotDispatched, match='Index not in observation'):
        await browser.execute(ACTION.validate_python({'kind': 'click', 'index': 2001}), view)
    assert not mutations(browser)


async def test_fingerprint_covers_text_beyond_visible_bound(browser):
    browser.raw.dom_state.llm_representation = lambda: 'z' * 100000 + 'one'
    first = await browser.observe()
    browser.raw.dom_state.llm_representation = lambda: 'z' * 100000 + 'two'
    second = await browser.observe()
    assert first['dom'] == second['dom']
    assert first['fingerprint'] != second['fingerprint']


@pytest.mark.parametrize('kind', ['click', 'input', 'key'])
async def test_foreign_form_destination_prevents_input(browser, kind):
    browser.fake.guard['form_url'] = 'https://other.example/submit'
    payload = {'kind': kind}
    if kind == 'key':
        payload['key'] = 'Enter'
    else:
        payload['index'] = 1
        if kind == 'input':
            payload['text'] = 'hello'
    with pytest.raises(NotDispatched):
        await execute(browser, payload)
    assert not mutations(browser)


@pytest.mark.parametrize('endpoint', ['', 'file:///tmp/chrome', 'http://user:password@localhost:9222'])
def test_missing_or_non_cdp_endpoint_cannot_launch_personal_browser(endpoint):
    with pytest.raises(ValueError, match='Explicit owned'):
        GeneralBrowser(endpoint, Task(url=URL, goal='form'), {})


async def test_start_only_connects_without_navigating(browser, monkeypatch):
    import sys

    session = browser.session
    session.start = AsyncMock()
    session.get_tabs = AsyncMock(return_value=browser.raw.tabs)
    captured_options = {}

    def construct(**options):
        captured_options.update(options)
        return session

    monkeypatch.setattr('decision_browser.general.session.make_session', construct)
    await browser.start()
    session.start.assert_awaited_once()
    assert not browser.fake.calls
    assert captured_options['cdp_url'] == 'http://owned-cdp:9222'
    assert captured_options['keep_alive'] is True
    assert captured_options['enable_default_extensions'] is False
    assert captured_options['auto_download_pdfs'] is False
    assert captured_options['downloads_path'] == '/tmp/bu-j-downloads'


async def test_blank_observation_only_before_first_explicit_navigation(browser):
    browser.session.get_current_page_url.return_value = 'about:blank'
    browser.session.get_all_frames.return_value = ({'frame': {'url': 'about:blank'}}, {})
    browser.raw.url = 'about:blank'
    browser.raw.tabs[0].url = 'about:blank'
    browser.raw.dom_state.selector_map = {}
    view = await browser.observe()
    assert view['url'] == 'about:blank'
    assert await browser.screenshot() == 'cG5n'
    assert not mutations(browser)
    await browser.execute(ACTION.validate_python({'kind': 'navigate', 'url': URL}), view)
    assert [m for _, m, _ in mutations(browser)] == ['navigate']
    with pytest.raises(NotDispatched):
        await browser.observe()


async def test_click_coordinates_come_from_cdp_not_frame_local_rect(browser):
    await execute(browser, {'kind': 'click', 'index': 1})
    mouse = [params for domain, _, params in mutations(browser) if domain == 'Input']
    assert len(mouse) == 2
    assert all(p['x'] == 150 and p['y'] == 220 for p in mouse)
    assert browser.fake.guard['x'] == 50
    assert browser.fake.guard['y'] == 30


async def test_geometry_failure_is_not_retried_or_dispatched(browser):
    browser.fake.fail = 'getContentQuads'
    with pytest.raises(NotDispatched, match='geometry validation'):
        await execute(browser, {'kind': 'click', 'index': 1})
    assert not mutations(browser)
    assert sum(m == 'getContentQuads' for _, m, _, _ in browser.fake.calls) == 1


@pytest.mark.parametrize('key, code', [('Tab', 9), ('Backspace', 8), ('ArrowDown', 40), ('Enter', 13), ('Space', 32), ('Control+a', 65)])
async def test_keys_include_native_key_codes_without_repeating_text_or_commands(browser, key, code):
    await execute(browser, {'kind': 'key', 'key': key})
    events = [params for _, method, params in mutations(browser) if method == 'dispatchKeyEvent']
    assert len(events) == 2
    assert events[0]['windowsVirtualKeyCode'] == code
    assert events[1]['type'] == 'keyUp'
    assert 'text' not in events[1] and 'commands' not in events[1]


async def test_content_pages_full_markdown_with_explicit_offsets(browser, monkeypatch):
    import sys

    extractor = AsyncMock(return_value=('a' * 100000 + 'last page', {}))
    monkeypatch.setitem(sys.modules, 'browser_use.dom.markdown_extractor', SimpleNamespace(extract_clean_markdown=extractor))
    browser.raw.title = 'Full page'
    browser.session.get_current_page_title = AsyncMock(return_value='Stale cached title')
    first = await browser.content()
    assert len(first['text']) == 100000
    assert first['total_characters'] == 100009
    assert first['truncated'] is True and first['next_start_char'] == 100000
    last = await browser.content(first['next_start_char'])
    assert last['text'] == 'last page'
    assert last['truncated'] is False and last['next_start_char'] is None
    assert last['url'] == URL and last['title'] == 'Full page'
    extractor.assert_awaited_with(browser_session=browser.session, extract_links=True, extract_images=True)
    with pytest.raises(ValueError, match='exceeds'):
        await browser.content(100010)
    assert not mutations(browser)


async def test_content_rejects_foreign_frames_before_extraction(browser, monkeypatch):
    import sys

    extractor = AsyncMock(return_value=('secret', {}))
    monkeypatch.setitem(sys.modules, 'browser_use.dom.markdown_extractor', SimpleNamespace(extract_clean_markdown=extractor))
    browser.session.get_all_frames.return_value = ({'frame': {'url': 'https://other.example/'}}, {})
    with pytest.raises(NotDispatched):
        await browser.content()
    extractor.assert_not_awaited()


async def test_upstream_synthetic_input_value_is_compared_to_live_property(browser):
    browser.node.attributes = {'type': 'checkbox', 'value': 'on'}
    browser.node.snapshot_node = SimpleNamespace(input_value='on', input_checked=False)
    browser.fake.guard.update(attrs={'type': 'checkbox'}, value='on', checked=False)
    await execute(browser, {'kind': 'click', 'index': 1})
    assert len(mutations(browser)) == 2


@pytest.mark.parametrize('change', [{'value': 'changed'}, {'checked': True}])
async def test_changed_live_input_state_is_rejected_after_snapshot(browser, change):
    browser.node.attributes = {'type': 'checkbox', 'value': 'on'}
    browser.node.snapshot_node = SimpleNamespace(input_value='on', input_checked=False)
    browser.fake.guard.update(attrs={'type': 'checkbox'}, value='on', checked=False)
    observed = await browser.observe()
    browser.fake.guard.update(change)
    with pytest.raises(NotDispatched, match='node changed'):
        await browser.execute(ACTION.validate_python({'kind': 'click', 'index': 1}), observed)
    assert not mutations(browser)
