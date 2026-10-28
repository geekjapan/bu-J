"""Scoped CDP transport adapter for the unchanged upstream Agent loop."""
from decision_browser.web.browser import Browser as ScopedBrowser, StalePage
from decision_browser.web.tasks import filter_page

TASK = ENDPOINT = None


class Browser(ScopedBrowser):
    def __init__(self, url):
        if TASK is None or url != TASK.url:
            raise ValueError('Baseline task must match its fixed scope')
        super().__init__(TASK, ENDPOINT, enforce_policy=False)

    def observe(self, screenshot=False):
        raw = super().observe(screenshot=screenshot)
        return filter_page(self.task, raw, raw['metadata'])

    def raw_observe(self):
        return super().observe(screenshot=False)
