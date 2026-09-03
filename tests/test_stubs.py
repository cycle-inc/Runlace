from __future__ import annotations

from runlace import stubs
from runlace.paths import RunlacePaths
from runlace.stubs import ConnectorSpec, ToolSpec, render_connector_stub
from runlace.typecheck import check_stubs


def tool(**overrides: object) -> ToolSpec:
    base: dict[str, object] = {
        "name": "echo",
        "method": "echo",
        "description": "Echoes back the input.",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        "output_schema": None,
        "risk": "read_only",
    }
    base.update(overrides)
    return ToolSpec(**base)  # type: ignore[arg-type]


def test_method_signature_is_keyword_only() -> None:
    source, _ = render_connector_stub(ConnectorSpec("everything", "everything", [tool()]))
    assert "def echo(self, *, message: str) -> object:" in source


def test_class_name_comes_from_the_ctx_attribute() -> None:
    source, _ = render_connector_stub(ConnectorSpec("my-server", "my_server", [tool()]))
    assert "class MyServer:" in source


def test_docstring_carries_description_and_risk() -> None:
    source, _ = render_connector_stub(ConnectorSpec("everything", "everything", [tool()]))
    assert "Echoes back the input." in source
    assert "(risk: read_only)" in source


def test_side_effect_risk_is_visible_in_the_stub() -> None:
    source, _ = render_connector_stub(
        ConnectorSpec("gmail", "gmail", [tool(name="send", method="send", risk="side_effect")])
    )
    assert "(risk: side_effect)" in source


def test_input_typed_dict_is_generated() -> None:
    source, _ = render_connector_stub(ConnectorSpec("everything", "everything", [tool()]))
    assert "class EchoInput(TypedDict):" in source
    assert "message: str" in source


def test_optional_parameters_get_a_stub_default() -> None:
    source, _ = render_connector_stub(
        ConnectorSpec(
            "s",
            "s",
            [
                tool(
                    input_schema={
                        "type": "object",
                        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                        "required": ["a"],
                    }
                )
            ],
        )
    )
    assert "def echo(self, *, a: str, b: int = ...) -> object:" in source


def test_reserved_word_parameter_is_renamed_and_documented() -> None:
    source, _ = render_connector_stub(
        ConnectorSpec(
            "pennylane",
            "pennylane",
            [
                tool(
                    name="list_transactions",
                    method="list_transactions",
                    input_schema={
                        "type": "object",
                        "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                        "required": ["from", "to"],
                    },
                )
            ],
        )
    )
    assert "def list_transactions(self, *, from_: str, to: str) -> object:" in source
    assert 'from_ maps to the JSON key "from".' in source


def test_tool_with_no_parameters_takes_no_arguments() -> None:
    source, _ = render_connector_stub(
        ConnectorSpec("s", "s", [tool(input_schema={"type": "object"})])
    )
    assert "def echo(self) -> object:" in source


def test_missing_output_schema_returns_object() -> None:
    source, _ = render_connector_stub(ConnectorSpec("s", "s", [tool()]))
    assert "-> object:" in source


def test_output_schema_becomes_the_return_type() -> None:
    source, _ = render_connector_stub(
        ConnectorSpec(
            "s",
            "s",
            [
                tool(
                    output_schema={
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                    }
                )
            ],
        )
    )
    assert "class EchoOutput(TypedDict):" in source
    assert "-> EchoOutput:" in source


def test_tool_with_an_unspellable_parameter_is_omitted_with_a_warning() -> None:
    source, warnings = render_connector_stub(
        ConnectorSpec(
            "s",
            "s",
            [tool(input_schema={"type": "object", "properties": {"content-type": {}}})],
        )
    )
    assert "def echo" not in source
    assert len(warnings) == 1
    assert "content-type" in warnings[0]


def test_connector_with_no_usable_tools_still_produces_a_valid_class() -> None:
    source, _ = render_connector_stub(ConnectorSpec("empty", "empty", []))
    assert source.endswith("class Empty:\n    ...\n")


def test_ctx_exposes_one_attribute_per_connector(paths: RunlacePaths) -> None:
    stubs.generate(
        paths,
        [
            ConnectorSpec("everything", "everything", [tool()]),
            ConnectorSpec("my-server", "my_server", [tool()]),
        ],
    )
    ctx = (paths.types / "ctx.pyi").read_text()
    assert "from .connectors.everything import Everything" in ctx
    assert "from .connectors.my_server import MyServer" in ctx
    assert "    everything: Everything" in ctx
    assert "    my_server: MyServer" in ctx
    assert "    inputs: dict[str, object]" in ctx


def test_generate_removes_stubs_for_connectors_that_went_away(paths: RunlacePaths) -> None:
    stubs.generate(paths, [ConnectorSpec("gone", "gone", [tool()])])
    assert (paths.connectors / "gone.pyi").exists()
    stubs.generate(paths, [ConnectorSpec("kept", "kept", [tool()])])
    assert not (paths.connectors / "gone.pyi").exists()
    assert (paths.connectors / "kept.pyi").exists()


# --- the part that matters: pyright has to accept what we generate ---------

AWKWARD = ConnectorSpec(
    "awkward",
    "awkward",
    [
        tool(name="no_args", method="no_args", input_schema={"type": "object"}),
        tool(
            name="reserved",
            method="reserved",
            input_schema={
                "type": "object",
                "properties": {"from": {"type": "string"}, "class": {"type": "integer"}},
                "required": ["from"],
            },
        ),
        tool(
            name="nested",
            method="nested",
            description='A description with "quotes",\na newline and a \\ backslash.',
            input_schema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "object",
                        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
                    },
                    "mode": {"type": "string", "enum": ["fast", "slow"]},
                    "maybe": {"type": ["string", "null"]},
                },
                "required": ["filter"],
            },
            output_schema={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        ),
        tool(name="untyped", method="untyped", input_schema=None, output_schema=None),
    ],
)


def test_generated_stubs_pass_pyright_strict(paths: RunlacePaths) -> None:
    stubs.generate(paths, [AWKWARD, ConnectorSpec("empty", "empty", [])])
    result = check_stubs(paths.types)
    assert result.ok, result.report()
