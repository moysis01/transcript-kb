"""Every AI request goes through call(): any provider and model (Settings → AI models), one shared budget of
requests at once, answers checked against their schema, failures retried.

A model is "provider/model" (tkb.config.PROVIDERS); a bare name is a Claude Code model, as before. Which
providers exist and how each is asked is in tkb.core.providers.
"""
import random
import time
from typing import Any

from tkb import config
from tkb.core import providers
from tkb.core.resources import slot


class LLMError(RuntimeError):
    pass


def call(system: str, prompt: str, *, schema: dict | None = None,
         model: str | None = None, retries: int | None = None, think: bool = True) -> Any:
    """Return text, or the parsed object when `schema` is given (always of that shape: a malformed answer is
    retried). `think=False` turns off extended thinking where a provider has it (Claude Code): for a choice made
    at a glance it can be 20x faster (a 300-title shortlist: 108 s with thinking, 5 s without)."""
    ref = config.model_ref(model) if model else config.get("model.default")
    provider, _, name = ref.partition("/")
    retries = retries or config.get("claude.retries")
    last = ""
    for attempt in range(retries):
        try:
            with slot("llm"):  # one budget for every concurrent job, Discover and Ask
                out = providers.ask(provider, name, system, prompt, schema, think)
            return conform(out, schema) if schema else out
        except providers.SetupError as e:  # a key, a model name, an address: retrying won't help
            raise LLMError(f"{ref}: {e}") from e
        except (providers.AIError, ValueError) as e:
            last = str(e)
        if attempt + 1 < retries:
            time.sleep(2 ** attempt * 2 * (0.7 + random.random() * 0.6))  # jitter: parallel retries don't stampede
    raise LLMError(f"{ref} failed after {retries} attempts: {last[:300]}")


def conform(value: Any, schema: dict, where: str = "answer") -> Any:
    """`value` checked against the parts of JSON Schema used here (type, properties, required, items, enum),
    with numbers given as strings accepted; ValueError says what's wrong."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{where}: expected an object")
        missing = [k for k in schema.get("required", []) if k not in value]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        props = schema.get("properties", {})
        return {k: conform(v, props[k], f"{where}.{k}") if k in props else v for k, v in value.items()}
    if kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{where}: expected a list")
        return [conform(v, schema.get("items", {}), f"{where}[{i}]") for i, v in enumerate(value)]
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{where}: expected a whole number")
        try:
            number = float(value)
        except ValueError as e:
            raise ValueError(f"{where}: expected a whole number") from e
        if number != int(number):
            raise ValueError(f"{where}: expected a whole number")
        value = int(number)
    elif kind == "number":
        try:
            value = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{where}: expected a number") from e
    elif kind == "string" and not isinstance(value, str):
        raise ValueError(f"{where}: expected text")
    elif kind == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{where}: expected true or false")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where}: {value!r} isn't one of the allowed values")
    return value
