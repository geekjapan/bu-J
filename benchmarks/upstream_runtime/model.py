"""Same live Azure inputs; baseline retains upstream confidence semantics."""
from decision_browser.web.provider import action_space, field_context

CLIENT = None


def choose(page, goal, history):
    return CLIENT.choose(page, goal, history)


def field_text(context):
    return CLIENT.field_text(context)
