from __future__ import annotations

import ipaddress
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        if (not url.isascii() or any(ord(c) <= 32 for c in url) or parsed.username is not None
                or parsed.password is not None or not host or "\\" in url):
            raise ValueError()
        fixture = parsed.scheme == "http" and host in {"127.0.0.1", "localhost"} and port == 8766
        if not fixture:
            if parsed.scheme != "https" or port not in (None, 443):
                raise ValueError()
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if not all(part and all(c.isalnum() or c == "-" for c in part) for part in host.split(".")):
                    raise ValueError()
            else:
                if not address.is_global or address.is_reserved or address.is_multicast:
                    raise ValueError()
        return f"{parsed.scheme}://{host}" + (f":{port}" if port and port != 443 else "")
    except (ValueError, TypeError):
        raise ValueError("public HTTPS URL required; only the owned fixture permits local HTTP") from None


class Acceptance(StrictModel):
    url: str | None = None
    text: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.url and not self.text:
            raise ValueError("acceptance needs url or text")
        return self


class Task(StrictModel):
    url: str = Field(min_length=1, max_length=8000)
    goal: str = Field(min_length=1, max_length=12000)
    allowed_origins: list[str] = Field(default_factory=list, max_length=30)
    max_steps: int = Field(default=20, ge=1, le=40)
    timeout_seconds: int = Field(default=300, ge=1, le=600)
    uploads: dict[str, str] = Field(default_factory=dict, max_length=10)
    acceptance: Acceptance | None = None
    output_schema: dict[str, Any] | None = None
    vision: bool = False

    @field_validator("output_schema")
    @classmethod
    def schema(cls, value):
        if value is not None:
            from jsonschema import Draft202012Validator
            def references(item):
                if isinstance(item, dict):
                    return bool({"$ref", "$dynamicRef", "$recursiveRef"} & item.keys()) or any(references(v) for v in item.values())
                return isinstance(item, list) and any(references(v) for v in item)
            if references(value):
                raise ValueError("output_schema references are unsupported")
            Draft202012Validator.check_schema(value)
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        origin(value)
        return value

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values):
        for value in values:
            if origin(value) != value:
                raise ValueError("allowed_origins must contain exact origins without paths")
        return values

    @model_validator(mode="after")
    def scope(self):
        allowed = self.allowed_origins or [origin(self.url)]
        if origin(self.url) not in allowed:
            raise ValueError("start URL outside allowed_origins")
        if self.acceptance and self.acceptance.url and origin(self.acceptance.url) not in allowed:
            raise ValueError("acceptance URL outside allowed_origins")
        object.__setattr__(self, "allowed_origins", allowed)
        return self

    def check_url(self, url: str):
        if origin(url) not in self.allowed_origins:
            raise ValueError("URL outside task allowed_origins")


class Navigate(StrictModel):
    kind: Literal["navigate"]
    url: str
    new_tab: bool = False


class Indexed(StrictModel):
    index: int = Field(ge=1)


class Click(Indexed):
    kind: Literal["click"]


class Input(Indexed):
    kind: Literal["input"]
    text: str = Field(max_length=4000)


class Select(Indexed):
    kind: Literal["select"]
    value: str = Field(max_length=2000)


class Upload(Indexed):
    kind: Literal["upload"]
    file: str = Field(min_length=1, max_length=200)


class Key(StrictModel):
    kind: Literal["key"]
    index: int | None = Field(default=None, ge=1)
    key: Literal["Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                 "Home", "End", "PageUp", "PageDown", "Backspace", "Delete", "Space", "Control+a"]


class Scroll(StrictModel):
    kind: Literal["scroll"]
    index: int | None = Field(default=None, ge=1)
    dx: int = Field(default=0, ge=-2000, le=2000)
    dy: int = Field(default=600, ge=-2000, le=2000)


class Tab(StrictModel):
    kind: Literal["switch_tab", "close_tab"]
    tab_id: str = Field(min_length=1, max_length=100)


class History(StrictModel):
    kind: Literal["back", "forward", "reload"]


class Wait(StrictModel):
    kind: Literal["wait"]
    seconds: float = Field(default=1, ge=0, le=3)


class Extract(StrictModel):
    kind: Literal["extract"]
    query: str = Field(min_length=1, max_length=2000)
    start_from_char: int = Field(default=0, ge=0, le=10_000_000)


class Screenshot(StrictModel):
    kind: Literal["screenshot"]


class Done(StrictModel):
    kind: Literal["done"]
    answer: str = Field(max_length=12000)


Action = Annotated[Navigate | Click | Input | Select | Upload | Key | Scroll | Tab | History | Wait | Extract | Screenshot | Done,
                   Field(discriminator="kind")]
ACTION = TypeAdapter(Action)


class Proposal(StrictModel):
    candidates: list[Action] = Field(min_length=1, max_length=3)
