"""Typed boundary for JSON decoders whose public return type is Any."""

import json
from typing import cast

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]


def decode_json(value: str) -> JSONValue:
    return cast(JSONValue, json.loads(value))


def decode_json_prefix(value: str) -> JSONValue:
    decoded, _ = json.JSONDecoder().raw_decode(value)
    return cast(JSONValue, decoded)
