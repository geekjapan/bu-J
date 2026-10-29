import base64
import json
import sqlite3

import pytest

from decision_browser.general.cli import main
from decision_browser.general.journal import GeneralJournal
from decision_browser.general.models import ACTION, Task
from decision_browser.general.runtime import run


URL = 'https://example.test/form'


def page(fingerprint='stable', *, url=URL, text='Form ready'):
    return {'fingerprint': fingerprint, 'url': url, 'text': text,
            'nodes': {'1': {'tag': 'input'}}, 'tabs': []}


class Browser:
    def __init__(self, observations=None, *, on_execute=None):
        self.observations = list(observations or [page()])
        self.executed = []
        self.starts = 0
        self.on_execute = on_execute

    async def start(self):
        self.starts += 1

    async def observe(self):
        return self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]

    async def screenshot(self):
        return base64.b64encode(b'offline screenshot').decode()

    async def execute(self, action, observed):
        self.executed.append(action.model_dump())
        if self.on_execute:
            self.on_execute(action, observed)
        return {'returned': True}


class Model:
    def __init__(self, payload, *, accepted=True):
        self.action = ACTION.validate_python(payload)
        self.accepted = accepted
        self.selections = 0

    def propose(self, task, observed, history, *, screenshot=None):
        return [self.action]

    def select(self, task, observed, history, candidates):
        self.selections += 1
        return {'accepted': self.accepted, 'choice': '0'}


@pytest.fixture
def journal(tmp_path):
    instance = GeneralJournal(tmp_path / 'journal.sqlite3')
    yield instance
    instance.close()


async def test_prepare_and_dispatch_are_committed_before_browser_effect(tmp_path):
    database = tmp_path / 'journal.sqlite3'
    observed_states = []

    def stored_state():
        with sqlite3.connect(database) as reader:
            return reader.execute('SELECT state FROM general_operations ORDER BY step DESC LIMIT 1').fetchone()[0]

    class CheckedJournal(GeneralJournal):
        def transition(self, task_id, step, state, result=None):
            if state == 'DISPATCHED':
                assert stored_state() == 'PREPARED'
                observed_states.append('PREPARED')
            super().transition(task_id, step, state, result)

    def effect(action, observed):
        assert stored_state() == 'DISPATCHED'
        observed_states.append('DISPATCHED')

    journal = CheckedJournal(database)
    browser = Browser(on_execute=effect)
    try:
        result = await run(Task(url=URL, goal='Click field', max_steps=1), 'durable', browser,
                           Model({'kind': 'click', 'index': 1}), journal, tmp_path)
        assert result['status'] == 'STEP_LIMIT'
        assert [action['kind'] for action in browser.executed] == ['navigate', 'click']
        assert observed_states == ['PREPARED', 'DISPATCHED', 'PREPARED', 'DISPATCHED']
        with sqlite3.connect(database) as reader:
            assert reader.execute('SELECT state FROM general_operations ORDER BY step').fetchall() == [
                ('ACKNOWLEDGED',), ('ACKNOWLEDGED',)]
    finally:
        journal.close()


async def test_effect_then_error_is_unknown_and_reused_task_cannot_execute_again(tmp_path):
    database = tmp_path / 'journal.sqlite3'
    task = Task(url=URL, goal='Submit form')
    model = Model({'kind': 'click', 'index': 1})

    def effect(action, observed):
        if action.kind == 'click':
            raise RuntimeError('connection lost after form submission')

    browser = Browser(on_execute=effect)
    journal = GeneralJournal(database)
    result = await run(task, 'no-replay', browser, model, journal, tmp_path)
    assert result['status'] == 'UNKNOWN_EFFECT'
    assert [action['kind'] for action in browser.executed] == ['navigate', 'click']
    journal.close()
    journal = GeneralJournal(database)
    retry_browser = Browser()
    try:
        with pytest.raises(ValueError, match='already used'):
            await run(task, 'no-replay', retry_browser, model, journal, tmp_path)
        assert retry_browser.starts == 0
        assert retry_browser.executed == []
        assert journal.db.execute('SELECT status FROM general_runs').fetchone() == ('UNKNOWN_EFFECT',)
        assert journal.db.execute('SELECT state FROM general_operations WHERE step=1').fetchone() == ('UNKNOWN_EFFECT',)
    finally:
        journal.close()


async def test_low_confidence_never_executes_chosen_input(tmp_path, journal):
    browser = Browser()
    model = Model({'kind': 'input', 'index': 1, 'text': 'must not be entered'}, accepted=False)
    result = await run(Task(url=URL, goal='Fill field'), 'abstain', browser, model, journal, tmp_path)
    assert result['status'] == 'ABSTAINED'
    assert [action['kind'] for action in browser.executed] == ['navigate']
    assert result['history'] == []


@pytest.mark.parametrize('payload', [
    {'kind': 'click', 'index': 999},
    {'kind': 'upload', 'index': 1, 'file': '../../private.txt'},
    {'kind': 'navigate', 'url': 'https://outside.test/'},
    {'kind': 'navigate', 'url': 'file:///etc/passwd'},
])
async def test_unsupported_candidate_is_rejected_before_selection_or_effect(tmp_path, journal, payload):
    browser = Browser()
    model = Model(payload)
    result = await run(Task(url=URL, goal='Use form'), 'invalid', browser, model, journal, tmp_path)
    assert result['status'] == 'FAILED'
    assert model.selections == 0
    assert [action['kind'] for action in browser.executed] == ['navigate']


async def test_page_changed_while_model_decides_prevents_input(tmp_path, journal):
    browser = Browser([page('initial'), page('captured'), page('changed')])
    model = Model({'kind': 'input', 'index': 1, 'text': 'old target'})
    result = await run(Task(url=URL, goal='Fill form', max_steps=1), 'stale', browser, model, journal, tmp_path)
    assert model.selections == 1
    assert result['status'] == 'STEP_LIMIT'
    assert [action['kind'] for action in browser.executed] == ['navigate']
    assert result['history'] == []


@pytest.mark.parametrize('acceptance,observed,status', [
    (None, page(), 'REPORTED'),
    ({'text': 'Saved'}, page(), 'ACCEPTANCE_FAILED'),
    ({'url': URL + '/done'}, page(text='Saved'), 'ACCEPTANCE_FAILED'),
    ({'url': URL, 'text': 'Saved'}, page(text='Saved successfully'), 'VERIFIED'),
])
async def test_done_requires_independent_acceptance_for_verified(tmp_path, journal, acceptance, observed, status):
    payload = {'url': URL, 'goal': 'Submit form'}
    if acceptance is not None:
        payload['acceptance'] = acceptance
    task = Task.model_validate(payload)
    browser = Browser([observed])
    result = await run(task, 'completion', browser, Model({'kind': 'done', 'answer': 'Submitted'}), journal, tmp_path)
    assert result['status'] == status
    assert result['completion']['verified'] is (status == 'VERIFIED')
    assert result['answer'] == 'Submitted'
    assert [action['kind'] for action in browser.executed] == ['navigate']
    assert journal.db.execute('SELECT status FROM general_runs').fetchone() == (status,)


@pytest.mark.parametrize('with_result', [False, True])
def test_existing_output_directory_remains_untouched(tmp_path, monkeypatch, capsys, with_result):
    task_file = tmp_path / 'input.json'
    task_file.write_text(json.dumps({'url': URL, 'goal': 'Read form'}))
    output = tmp_path / 'existing'
    output.mkdir()
    (output / 'task.json').write_text('previous task evidence')
    if with_result:
        (output / 'result.json').write_text('previous result evidence')
    before = {path.name: path.read_bytes() for path in output.iterdir()}

    async def forbidden(*args):
        pytest.fail('preexisting output must be rejected before browser execution')

    monkeypatch.setattr('decision_browser.general.cli.execute', forbidden)
    assert main(['--task', str(task_file), '--output', str(output), '--launch']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'FAILED'
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


async def test_expired_planning_does_not_start_another_model_request(tmp_path, journal, monkeypatch):
    clock = [0]
    monkeypatch.setattr('decision_browser.general.runtime.time.monotonic', lambda: clock[0])
    class SlowModel(Model):
        def propose(self, *args, **kwargs):
            clock[0] = 2
            return [self.action]
    model = SlowModel({'kind': 'input', 'index': 1, 'text': 'Ada'})
    browser = Browser()
    result = await run(Task(url=URL, goal='Fill name', timeout_seconds=1), 'deadline', browser, model, journal, tmp_path)
    assert result['status'] == 'TIMED_OUT'
    assert model.selections == 0
    assert [a['kind'] for a in browser.executed] == ['navigate']


async def test_schema_requires_real_extraction_before_done(tmp_path, journal):
    task = Task(url=URL, goal='Extract name', output_schema={'type': 'object'}, acceptance={'text': 'Form ready'})
    result = await run(task, 'structured', Browser(), Model({'kind': 'done', 'answer': 'Done'}), journal, tmp_path)
    assert result['status'] == 'ACCEPTANCE_FAILED'
    assert result['completion']['verified'] is False
