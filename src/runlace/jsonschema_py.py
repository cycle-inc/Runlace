"""JSON Schema to Python type expressions.

Each connector stub gets one :class:`TypeRenderer`. Calling :meth:`render`
returns a type expression as source text and, as a side effect, collects any
``TypedDict`` classes that expression depends on plus the ``typing`` names the
file has to import.

Anything the mapper does not understand becomes ``object``. That is the honest
answer -- SKILL.md tells the agent to narrow explicitly -- and it keeps pyright
strict-clean instead of asserting a type we cannot back up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .naming import class_name, param_name

_PRIMITIVES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "null": "None",
}

# Guards against schemas that reference themselves through $ref. Public because
# `keys` walks values along the same schemas and has to stop at the same place.
MAX_DEPTH = 12


def defs_of(schema: dict[str, Any], inherited: dict[str, Any]) -> dict[str, Any]:
    """The ``$defs``/``definitions`` visible from *schema*, innermost winning."""
    merged = dict(inherited)
    for key in ("$defs", "definitions"):
        local = schema.get(key)
        if isinstance(local, dict):
            merged.update(local)
    return merged


@dataclass
class Field:
    """One property of an object schema, as it will be spelled in Python."""

    json_key: str
    py_name: str
    type_expr: str
    required: bool

    @property
    def renamed(self) -> bool:
        return self.py_name != self.json_key


@dataclass
class InputShape:
    fields: list[Field]
    # Set when a property name cannot be spelled as a keyword argument.
    unmappable: str | None = None


@dataclass
class TypeRenderer:
    """Accumulates the TypedDicts and imports needed by one stub file."""

    typing_imports: set[str] = field(default_factory=set[str])
    blocks: list[str] = field(default_factory=list[str])
    _used_names: set[str] = field(default_factory=set[str])
    _ref_names: dict[str, str] = field(default_factory=dict[str, str])

    # -- public API ------------------------------------------------------

    def render(self, schema: Any, hint: str, defs: dict[str, Any] | None = None) -> str:
        return self._render(schema, hint, defs or {}, depth=0)

    def object_fields(
        self, schema: Any, hint: str, defs: dict[str, Any] | None = None
    ) -> InputShape:
        """Flatten an object schema into ordered, Python-spellable fields."""
        if not isinstance(schema, dict):
            return InputShape(fields=[])
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return InputShape(fields=[])

        required = schema.get("required")
        required_keys = set(required) if isinstance(required, list) else set()
        resolved_defs = defs_of(schema, defs or {})

        fields: list[Field] = []
        for key, subschema in properties.items():
            py_name = param_name(str(key))
            if py_name is None:
                return InputShape(fields=[], unmappable=str(key))
            type_expr = self._render(
                subschema, f"{hint}{class_name(str(key))}", resolved_defs, depth=1
            )
            fields.append(
                Field(
                    json_key=str(key),
                    py_name=py_name,
                    type_expr=type_expr,
                    required=key in required_keys,
                )
            )
        # Required first, so signatures read naturally.
        fields.sort(key=lambda f: not f.required)
        return InputShape(fields=fields)

    def add_typed_dict(self, name: str, fields: list[Field], doc: str | None = None) -> str:
        """Emit a TypedDict class block and return the (deduplicated) name."""
        unique = self._unique_name(name)
        self.typing_imports.add("TypedDict")
        lines = [f"class {unique}(TypedDict):"]
        if doc:
            lines.append(indent_docstring(doc, "    "))
        for f in fields:
            annotation = f.type_expr
            if not f.required:
                self.typing_imports.add("NotRequired")
                annotation = f"NotRequired[{annotation}]"
            comment = f'  # JSON key "{f.json_key}"' if f.renamed else ""
            lines.append(f"    {f.py_name}: {annotation}{comment}")
        self.blocks.append("\n".join(lines))
        return unique

    # -- internals -------------------------------------------------------

    def _unique_name(self, base: str) -> str:
        name = base
        counter = 2
        while name in self._used_names:
            name = f"{base}{counter}"
            counter += 1
        self._used_names.add(name)
        return name

    def _render(self, schema: Any, hint: str, defs: dict[str, Any], depth: int) -> str:
        if depth > MAX_DEPTH or not isinstance(schema, dict) or not schema:
            return "object"

        defs = defs_of(schema, defs)

        ref = schema.get("$ref")
        if isinstance(ref, str):
            return self._render_ref(ref, defs, depth)

        if "const" in schema:
            literal = _literal(schema["const"])
            if literal is not None:
                self.typing_imports.add("Literal")
                return f"Literal[{literal}]"
            return "object"

        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            literals = [_literal(v) for v in enum]
            if all(lit is not None for lit in literals):
                self.typing_imports.add("Literal")
                return f"Literal[{', '.join(str(lit) for lit in literals)}]"
            return "object"

        for combinator in ("anyOf", "oneOf"):
            members = schema.get(combinator)
            if isinstance(members, list) and members:
                return self._union(
                    [self._render(m, hint, defs, depth + 1) for m in members]
                )

        all_of = schema.get("allOf")
        if isinstance(all_of, list) and len(all_of) == 1:
            return self._render(all_of[0], hint, defs, depth + 1)

        type_ = schema.get("type")
        if isinstance(type_, list):
            return self._union(
                [self._render({**schema, "type": t}, hint, defs, depth + 1) for t in type_]
            )

        if type_ == "array":
            items = schema.get("items")
            if isinstance(items, list):  # tuple-style arrays: not worth modelling
                return "list[object]"
            return f"list[{self._render(items, f'{hint}Item', defs, depth + 1)}]"

        if type_ == "object" or (type_ is None and "properties" in schema):
            return self._render_object(schema, hint, defs, depth)

        if isinstance(type_, str) and type_ in _PRIMITIVES:
            return _PRIMITIVES[type_]

        return "object"

    def _render_ref(self, ref: str, defs: dict[str, Any], depth: int) -> str:
        known = self._ref_names.get(ref)
        if known is not None:
            return known
        name = ref.rsplit("/", 1)[-1]
        target = defs.get(name)
        if not isinstance(target, dict):
            return "object"
        # Reserve the name before recursing so a self-reference resolves to it.
        placeholder = self._unique_name(class_name(name))
        self._used_names.discard(placeholder)
        self._ref_names[ref] = placeholder
        rendered = self._render(target, class_name(name), defs, depth + 1)
        self._ref_names[ref] = rendered
        return rendered

    def _render_object(
        self, schema: dict[str, Any], hint: str, defs: dict[str, Any], depth: int
    ) -> str:
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                value = self._render(additional, f"{hint}Value", defs, depth + 1)
                return f"dict[str, {value}]"
            return "dict[str, object]"

        required = schema.get("required")
        required_keys = set(required) if isinstance(required, list) else set()

        fields: list[Field] = []
        for key, subschema in properties.items():
            py_name = param_name(str(key))
            if py_name is None:
                # One unspellable key makes the whole shape untyped rather than
                # silently dropping a property.
                return "dict[str, object]"
            fields.append(
                Field(
                    json_key=str(key),
                    py_name=py_name,
                    type_expr=self._render(
                        subschema, f"{hint}{class_name(str(key))}", defs, depth + 1
                    ),
                    required=key in required_keys,
                )
            )

        description = schema.get("description")
        return self.add_typed_dict(
            class_name(hint),
            fields,
            doc=description if isinstance(description, str) else None,
        )

    @staticmethod
    def _union(members: list[str]) -> str:
        if "object" in members:
            return "object"
        seen: list[str] = []
        for m in members:
            if m not in seen:
                seen.append(m)
        if not seen:
            return "object"
        if len(seen) == 1:
            return seen[0]
        return " | ".join(seen)


def _literal(value: Any) -> str | None:
    """Render a value as a ``Literal`` member, or ``None`` if it cannot be one."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (str, int)):
        return repr(value)
    return None


def indent_docstring(text: str, indent: str) -> str:
    """Render text as a triple-quoted docstring at the given indentation."""
    body = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"').strip()
    lines = body.splitlines() or [""]
    if len(lines) == 1:
        return f'{indent}"""{lines[0]}"""'
    inner = "\n".join(f"{indent}{line}".rstrip() for line in lines)
    return f'{indent}"""\n{inner}\n{indent}"""'
