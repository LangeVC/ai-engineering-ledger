"""Minimal JSON Schema 2020-12 subset validator (jsonschema is not installed).
Supports: $ref (local/relative file), type, const, enum, pattern, min/max, minLength,
minItems, items, properties, required, additionalProperties(false), anyOf, not, if/then/else."""
import json, re, os

class Loader:
    def __init__(self, root): self.root, self.cache = root, {}
    def doc(self, name):
        if name not in self.cache:
            with open(os.path.join(self.root, name), encoding="utf-8") as f: self.cache[name] = json.load(f)
        return self.cache[name]

def _t(v, t):
    if t == "object": return isinstance(v, dict)
    if t == "array": return isinstance(v, list)
    if t == "string": return isinstance(v, str)
    if t == "boolean": return isinstance(v, bool)
    if t == "null": return v is None
    if t == "integer": return isinstance(v, int) and not isinstance(v, bool)
    if t == "number": return isinstance(v, (int, float)) and not isinstance(v, bool)
    raise ValueError(t)

def _resolve(ref, docname, L):
    f, _, frag = ref.partition("#")
    name = f or docname
    node = L.doc(name)
    for p in [x for x in frag.split("/") if x]: node = node[p]
    return node, name

def errors(inst, schema, docname, L, path="$"):
    e = []
    if "$ref" in schema:
        node, n = _resolve(schema["$ref"], docname, L)
        e += errors(inst, node, n, L, path)
    if "type" in schema:
        ts = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_t(inst, t) for t in ts): return e + [f"{path}: type {ts}"]
    if "const" in schema and inst != schema["const"]: e.append(f"{path}: const {schema['const']!r}")
    if "enum" in schema and inst not in schema["enum"]: e.append(f"{path}: enum")
    if isinstance(inst, str):
        if "pattern" in schema and not re.search(schema["pattern"], inst): e.append(f"{path}: pattern")
        if "minLength" in schema and len(inst) < schema["minLength"]: e.append(f"{path}: minLength")
    if _t(inst, "number"):
        if "minimum" in schema and inst < schema["minimum"]: e.append(f"{path}: minimum")
        if "maximum" in schema and inst > schema["maximum"]: e.append(f"{path}: maximum")
    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]: e.append(f"{path}: minItems")
        if "items" in schema:
            for i, x in enumerate(inst): e += errors(x, schema["items"], docname, L, f"{path}[{i}]")
    if isinstance(inst, dict):
        for r in schema.get("required", []):
            if r not in inst: e.append(f"{path}: missing required '{r}'")
        props = schema.get("properties", {})
        for k, v in inst.items():
            if k in props: e += errors(v, props[k], docname, L, f"{path}.{k}")
            elif schema.get("additionalProperties") is False: e.append(f"{path}: unexpected property '{k}'")
    if "anyOf" in schema and not any(not errors(inst, s, docname, L, path) for s in schema["anyOf"]):
        e.append(f"{path}: anyOf")
    if "not" in schema and not errors(inst, schema["not"], docname, L, path): e.append(f"{path}: not")
    if "if" in schema:
        branch = "then" if not errors(inst, schema["if"], docname, L, path) else "else"
        if branch in schema: e += errors(inst, schema[branch], docname, L, path)
    return e

def validate(inst, docname, L): return errors(inst, L.doc(docname), docname, L)
