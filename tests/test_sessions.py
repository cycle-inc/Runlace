"""What a tool call hands back to the workflow.

A server that declares an output schema answers with `structuredContent` and
there is nothing to decide. The interesting case is the other one, which is not
an edge case at all: of the four servers this project has been tried against,
`files`, `memory` and `thinking` declare a schema on every tool (24/24) and
GitHub declares one on none (0/47). The second group still answers with JSON --
just serialised into a text block, because that is the only place the protocol
leaves for it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents

from runlace.sessions import tool_payload


def text(*blocks: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=b) for b in blocks])


def test_structured_content_wins_and_is_never_reinterpreted() -> None:
    """It is what the stub promised; nothing here may second-guess it."""
    result = CallToolResult(
        content=[TextContent(type="text", text="ignored")],
        structured_content={"total": 3},
    )
    assert tool_payload(result) == {"total": 3}


# -- a lone text block holding JSON ----------------------------------------


def test_a_json_object_in_one_text_block_is_parsed() -> None:
    payload = tool_payload(text(json.dumps({"login": "octocat", "id": 1})))
    assert payload == {"login": "octocat", "id": 1}


def test_a_json_array_in_one_text_block_is_parsed() -> None:
    payload = tool_payload(text(json.dumps([{"sha": "abc"}, {"sha": "def"}])))
    assert payload == [{"sha": "abc"}, {"sha": "def"}]


@pytest.mark.parametrize(
    "raw",
    [
        "42",  # an id, and ids that start with a zero do not survive int()
        '"already a string"',
        "true",
        "null",
        "1.0",  # a version, not a float
    ],
)
def test_a_json_scalar_is_left_as_the_string_it_was(raw: str) -> None:
    """Parsing these would be corruption dressed as convenience.

    A tool answering `42` may well mean the four-then-two characters. Objects
    and arrays are unambiguous -- no tool returns the *text* `{"a": 1}` and
    means it -- so the line is drawn there rather than at "is it valid JSON".
    """
    assert tool_payload(text(raw)) == raw


def test_free_text_is_left_alone() -> None:
    assert tool_payload(text("successfully downloaded README.md")) == (
        "successfully downloaded README.md"
    )


def test_something_that_merely_starts_like_json_is_left_alone() -> None:
    assert tool_payload(text('{"unterminated": ')) == '{"unterminated": '


# -- more than one block ---------------------------------------------------


def test_several_text_blocks_stay_a_list_of_strings() -> None:
    """Concatenating or parsing them would invent a structure the server did not send."""
    assert tool_payload(text('{"a": 1}', '{"b": 2}')) == ['{"a": 1}', '{"b": 2}']


def test_a_mixed_result_is_dumped_block_by_block() -> None:
    """GitHub's `get_file_contents` really does answer like this.

    A prose block followed by the resource. Had `_parsed` looked at the first
    block alone it would have handed the workflow the sentence and dropped the
    file, which is why parsing is confined to results that are *one* block.
    """
    result = CallToolResult(
        content=[
            TextContent(type="text", text="successfully downloaded text file"),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri="https://example.invalid/README.md",
                    mime_type="text/markdown",
                    text="# Title",
                ),
            ),
        ]
    )
    payload: Any = tool_payload(result)
    assert isinstance(payload, list) and len(payload) == 2
    assert payload[0]["type"] == "text"
    assert payload[1]["resource"]["text"] == "# Title"


def test_an_empty_result_is_an_empty_list() -> None:
    assert tool_payload(CallToolResult(content=[])) == []
