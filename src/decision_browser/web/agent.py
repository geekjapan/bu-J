
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import sqlite3
import time

from .browser import NotDispatched
from .journal import WebJournal
from .tasks import filter_page, verify_task


class WebAgent:
    MAX_ACTIONS = 60
    MAX_DECISIONS = 120
    DEADLINE_SECONDS = 120

    def __init__(self, task, browser, model, journal_path, task_id):
        self.task, self.browser, self.model = task, browser, model
        self.task_id = task_id
        self.journal = WebJournal(journal_path)
        self.history, self.decisions, self.text_calls = [], [], []
        self.started_at = None
        self.completed_at = None
        self.cdp_start = Counter()
        self.cdp_end = None
        self.pending_text = None
        self.result = None

    def elapsed(self):
        stopped_at = self.completed_at if self.completed_at is not None else time.monotonic()
        return round((stopped_at - self.started_at) * 1000) if self.started_at is not None else 0

    def finish(self, status, reason, **extra):
        end_counts = self.cdp_end if self.cdp_end is not None else Counter(getattr(self.browser, "cdp_counts", {}))
        self.result = dict(status=status, reason=reason, elapsed_ms=self.elapsed(),
                           history=self.history, decisions=self.decisions, text_calls=self.text_calls,
                           calls=deepcopy(self.model.calls), model_text_calls=deepcopy(self.model.text_calls),
                           total_tokens=self.model.total_tokens,
                           decision_count=len(self.decisions), action_count=len(self.history),
                           cdp_counts_run=dict(end_counts - self.cdp_start),
                           cdp_counts_run_bounds="First prediction through accepted DONE; setup and independent verification excluded", **extra)
        try:
            self.journal.event(self.task_id, "RUN_FINISHED", **self.result)
        except sqlite3.Error as error:
            self.result.update(status="UNKNOWN_EFFECT" if status == "UNKNOWN_EFFECT" else "FAILED",
                               reason="AUDIT_PERSISTENCE_FAILED", exception_type=type(error).__name__)
        return self.result

    def expired(self):
        return time.monotonic() - self.started_at >= self.DEADLINE_SECONDS

    def fresh(self, page, action=None):
        try:
            return self.browser.fresh(page, action)
        except Exception as error:
            if type(error).__name__ == "StalePage":
                return False
            raise

    @staticmethod
    def effect_verified(action, before, after, text):
        kind = action["kind"]
        if kind == "wait":
            return True
        if kind == "scroll":
            old_y, new_y = before.get("scroll", {}).get("y"), after.get("scroll", {}).get("y")
            return (isinstance(old_y, (int, float)) and isinstance(new_y, (int, float))
                    and (new_y - old_y) * action.get("delta", 0) > 0)
        old_key, new_key = before.get("page_key"), after.get("page_key")
        same_document = (isinstance(old_key, list) and isinstance(new_key, list)
                         and bool(old_key) and bool(new_key) and old_key[0] == new_key[0])
        if kind in {"fill", "select"}:
            if not same_document:
                return False
            node, expected = action.get("node"), text if kind == "fill" else action.get("value")
            guard = after.get("guards", {}).get(str(node))
            if isinstance(guard, list) and len(guard) > 3 and guard[0] == node:
                if kind == "select" or guard[3] == expected:
                    return guard[3] == expected
            direct = any(a.get("node") == node and a.get("kind") == kind
                       and a.get("value" if kind == "fill" else "current_value") == expected
                       for a in after.get("actions", []))
            if direct or kind != 'fill':
                return direct
            original = before.get('metadata', {}).get(str(node), {})
            identity = original.get('attributes', {}).get('id')
            if not identity or original.get('tag') not in {'input', 'textarea'}:
                return False
            if sum(meta.get('attributes', {}).get('id') == identity
                   for meta in before.get('metadata', {}).values()) != 1:
                return False
            if sum(meta.get('attributes', {}).get('id') == identity
                   for meta in after.get('metadata', {}).values()) != 1:
                return False
            replacements = [(a, after.get('metadata', {}).get(str(a.get('node')), {}))
                            for a in after.get('actions', []) if a.get('kind') == 'fill']
            matches = [(a, meta) for a, meta in replacements
                       if meta.get('attributes', {}).get('id') == identity]
            return (len(matches) == 1 and matches[0][0].get('value') == expected
                    and str(matches[0][0].get('node')) not in before.get('metadata', {})
                    and all(matches[0][1].get(key) == original.get(key)
                            for key in ('tag', 'input_type', 'form_action', 'form_method')))
        if kind == "click":
            return (any(before.get(key) is not None and after.get(key) is not None
                        and before[key] != after[key] for key in ("url", "fingerprint"))
                    or (isinstance(old_key, list) and isinstance(new_key, list)
                        and bool(old_key) and bool(new_key) and old_key[0] != new_key[0])
                    or (same_document and any(node in after.get("guards", {}) and guard != after["guards"][node]
                                             for node, guard in before.get("guards", {}).items())))
        return False

    def run(self):
        if self.result is not None:
            return self.result
        dispatched_step = None
        attempted_step = 0
        stale_reads = 0
        result_wait_deadline = None
        result_wait_reads = 0
        try:
            if self.journal.unresolved(self.task_id):
                self.journal.recover(self.task_id)
                return self.finish("UNKNOWN_EFFECT", "RESTART_RECONCILIATION_REQUIRED")
            page = self.browser.observe()
            self.cdp_start = Counter(getattr(self.browser, "cdp_counts", {}))
            self.started_at = time.monotonic()
            while True:
                if self.expired():
                    return self.finish("FAILED", "DEADLINE_EXCEEDED")
                if len(self.decisions) >= self.MAX_DECISIONS:
                    return self.finish("FAILED", "DECISION_BUDGET_EXCEEDED")
                if not self.fresh(page):
                    page = self.browser.observe()
                    stale_reads += 1
                    if stale_reads >= self.MAX_DECISIONS:
                        return self.finish("FAILED", "PREFLIGHT_BUDGET_EXCEEDED")
                    continue
                if page.get("omitted_actions", 0) > 0:
                    return self.finish("UNSUPPORTED", "TRUNCATED_ACTIONS")
                candidates = filter_page(self.task, page, page.get("metadata", {}))
                checks = candidates.get('completion', {}).get('checks', {})
                if (getattr(self.task, 'name', '') == 'flights' and self.history
                        and self.history[-1].get('action') == 'Search'
                        and all(checks.get(k) for k in ('search_page', 'origin', 'destination', 'date', 'year', 'one_way'))
                        and not checks.get('results')):
                    if result_wait_deadline is None:
                        result_wait_deadline = time.monotonic() + 5
                    if result_wait_reads < 100 and time.monotonic() < result_wait_deadline:
                        result_wait_reads += 1
                        time.sleep(.05)
                        page = self.browser.observe()
                        continue
                decision = self.model.choose(candidates, self.task.goal, self.history)
                self.decisions.append({**deepcopy(decision), "fingerprint": page.get("fingerprint"),
                                       "elapsed_ms": self.elapsed()})
                self.journal.event(self.task_id, "DECISION", **self.decisions[-1])
                if self.expired():
                    return self.finish("FAILED", "DEADLINE_EXCEEDED")
                if not self.fresh(page):
                    page = self.browser.observe()
                    continue
                if decision.get("accepted") is not True:
                    return self.finish("ESCALATED", "DECISION_ABSTAINED")
                selected = decision["choice"]
                if selected in {"DONE", "BLOCKED"}:
                    if not self.fresh(page):
                        page = self.browser.observe()
                        continue
                    if selected == "BLOCKED":
                        return self.finish("FAILED", "MODEL_BLOCKED")
                    if self.expired():
                        return self.finish("FAILED", "DEADLINE_EXCEEDED")
                    self.completed_at = time.monotonic()
                    self.cdp_end = Counter(getattr(self.browser, "cdp_counts", {}))
                    observed = self.browser.observe()
                    if not self.fresh(observed):
                        return self.finish("FAILED", "FINAL_OBSERVATION_STALE")
                    verification = verify_task(self.task, observed)
                    return self.finish("SUCCEEDED" if verification["passed"] else "FAILED",
                                       "VERIFIED_DONE" if verification["passed"] else "DONE_NOT_VERIFIED",
                                       verification=verification)
                action = next((a for a in candidates["actions"] if a["id"] == selected), None)
                if action is None:
                    return self.finish("FAILED", "INVALID_ACTION_CHOICE")
                if len(self.history) >= self.MAX_ACTIONS:
                    return self.finish("FAILED", "ACTION_BUDGET_EXCEEDED")
                if not self.fresh(page, action):
                    page = self.browser.observe()
                    continue
                text, helper = None, None
                if action["kind"] == "fill":
                    context = deepcopy(self.model.field_context(self.task.goal, action, candidates, self.history))
                    if self.pending_text is not None and self.pending_text[0] == context:
                        _, text, helper = self.pending_text
                    else:
                        text, helper = self.model.field_text(context)
                        self.pending_text = (context, text, deepcopy(helper))
                        self.text_calls.append({**deepcopy(helper), "field": action.get("label"), "value": text})
                        self.journal.event(self.task_id, "TEXT_GENERATED", **self.text_calls[-1])
                    if self.expired():
                        return self.finish("FAILED", "DEADLINE_EXCEEDED")
                    if not self.fresh(page, action):
                        page = self.browser.observe()
                        continue
                if self.expired():
                    return self.finish("FAILED", "DEADLINE_EXCEEDED")
                attempted_step += 1
                step = attempted_step
                self.journal.prepare(self.task_id, step, action, page)
                self.journal.transition(self.task_id, step, "DISPATCHED")
                dispatched_step = step
                try:
                    self.browser.act(action, page, text=text)
                except NotDispatched:
                    self.journal.transition(self.task_id, step, "NOT_SENT", "PRE_INPUT_STALE")
                    dispatched_step = None
                    self.journal.event(self.task_id, "ACTION_NOT_SENT", step=step, action=action.get("label"),
                                       reason="PRE_INPUT_STALE", elapsed_ms=self.elapsed())
                    page = self.browser.observe()
                    continue
                self.pending_text = None
                entry = dict(step=step, action=action.get("label"), kind=action["kind"], choice=selected,
                             operation=decision.get("operation"), target=decision.get("target"),
                             confidence=decision.get("confidence"), probabilities=decision.get("probabilities"),
                             latency_ms=decision.get("latency_ms"), usage=decision.get("usage"), text=text,
                             text_helper=helper, url=page.get("url"), page_changed=None,
                             executed_ms=self.elapsed(), elapsed_ms=self.elapsed())
                self.history.append(entry)
                self.journal.event(self.task_id, "ACTION_EXECUTED", **entry)
                settle_deadline = min(time.monotonic() + 2, self.started_at + self.DEADLINE_SECONDS)
                entry.update(effect_verified=False, observation_count=0)
                for attempt in range(40):
                    if time.monotonic() >= settle_deadline:
                        break
                    entry["observation_count"] += 1
                    try:
                        observed = self.browser.observe()
                        entry.update(page_changed=observed.get("fingerprint") != page.get("fingerprint"),
                                     url=observed.get("url"), elapsed_ms=self.elapsed(),
                                     observation={k: deepcopy(observed.get(k)) for k in ("url", "fingerprint", "page_key")})
                        entry["effect_verified"] = (self.fresh(observed) and not self.expired()
                                                    and self.effect_verified(action, page, observed, text))
                    except Exception as error:
                        if type(error).__name__ != "StalePage":
                            raise
                    if entry["effect_verified"]:
                        break
                    remaining = settle_deadline - time.monotonic()
                    if attempt < 39 and remaining > 0:
                        time.sleep(min(.05, remaining))
                entry["elapsed_ms"] = self.elapsed()
                self.journal.event(self.task_id, "ACTION_OBSERVED", **entry)
                if not entry["effect_verified"]:
                    raise RuntimeError("Action effect was not independently observed")
                self.journal.transition(self.task_id, step, "VERIFIED")
                dispatched_step = None
                page = observed
        except Exception as error:
            exception_type = type(error).__name__
            if dispatched_step is not None:
                try:
                    self.journal.transition(self.task_id, dispatched_step, "UNKNOWN_EFFECT", exception_type)
                except Exception:
                    pass
                return self.finish("UNKNOWN_EFFECT", "ACTION_EFFECT_UNCERTAIN", exception_type=exception_type)
            return self.finish("FAILED", "RUNTIME_ERROR", exception_type=exception_type)

    def close(self):
        self.journal.close()
