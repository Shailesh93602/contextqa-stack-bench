#!/usr/bin/env python3
"""Score ContextQA benchmark runs. Python 3 standard library only, no network.

    python3 scorer/score.py --check                        # validate apps/*/recipe.yaml + answer-key.yaml
    python3 scorer/score.py results/<file>.jsonl           # score held-out items (default)
    python3 scorer/score.py results/<file>.jsonl --split all --n 5

The rules are in README.md. The code below is the only definition of a score.
"""
import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The only correct verdict per (capability, version). A row's own `expected`/`correct` is cross-checked, never trusted.
EXPECTED = {
    ("repro", "bug"): "REPRODUCED",
    ("repro", "fixed"): "NOT_REPRODUCED",
    ("verify_fix", "bug"): "NOT_FIXED",
    ("verify_fix", "fixed"): "FIXED",
}
CAPABILITIES = ("repro", "verify_fix")

# Pre-registered headline weights (method §3.7). Change only with a new scorecard version.
# Capabilities this scorer cannot measure yet count as 0 and cap the band at Partial.
WEIGHTS = {"repro": 0.25, "verify_fix": 0.15, "pr_impact": 0.20,
           "generated_tests": 0.15, "consistency": 0.15, "api_db": 0.10}


# ---------- statistics ----------

def wilson(k, n, z=1.96):
    """Wilson score 95% interval for k successes out of n. None when n == 0."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pct_floor(x):
    return int(math.floor(x * 100 + 1e-9))


def pct_ceil(x):
    return int(math.ceil(x * 100 - 1e-9))


def fmt_ci(k, n):
    """'≥ 88% (30/30, CI 88–100%)'. The lower bound is rounded down, so 100% is never the headline."""
    ci = wilson(k, n)
    if ci is None:
        return "not measured"
    return f"≥ {pct_floor(ci[0])}% ({k}/{n}, CI {pct_floor(ci[0])}–{pct_ceil(ci[1])}%)"


def band(lower, stable_share, all_measured=True):
    """Works / Partial / Not yet (method §3.7). Works also needs consistency measured and >= 80% stable."""
    if lower is None:
        return "Not measured"
    works = lower >= 0.80 and stable_share is not None and stable_share >= 0.80 and all_measured
    if works:
        return "Works"
    return "Partial" if lower >= 0.50 else "Not yet"


# ---------- YAML subset loader (block maps/lists, flow lists of scalars, quoted or plain scalars) ----------

_KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*):(?:\s+(.*))?$")


def _strip_comment(s):
    quote, escaped = None, False
    for i, ch in enumerate(s):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.rstrip()


def _scalar(s):
    s = s.strip()
    if s in ("", "~", "null"):
        return None
    if s in ("true", "false"):
        return s == "true"
    if s.startswith('"'):
        return json.loads(s)
    if s.startswith("'"):
        return s[1:-1].replace("''", "'")
    if s == "{}":
        return {}
    if s.startswith("["):
        inner = s[1:-1].strip()
        return [_scalar(x) for x in _split_flow(inner)] if inner else []
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _split_flow(s):
    parts, cur, quote = [], "", None
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote, cur = ch, cur + ch
        elif ch == ",":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def load_yaml(text):
    lines = []
    for raw in text.splitlines():
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ValueError("tabs are not allowed for indentation")
        body = _strip_comment(raw)
        if body.strip():
            lines.append([len(body) - len(body.lstrip()), body.strip()])

    def block(i, indent):
        if lines[i][1] == "-" or lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == indent and (lines[i][1] == "-" or lines[i][1].startswith("- ")):
                rest = lines[i][1][1:].strip()
                if not rest:
                    val, i = block(i + 1, lines[i + 1][0])
                elif _KEY.match(rest):
                    lines[i] = [indent + 2, rest]
                    val, i = block(i, indent + 2)
                else:
                    val, i = _scalar(rest), i + 1
                out.append(val)
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == indent:
            m = _KEY.match(lines[i][1])
            if not m:
                raise ValueError(f"cannot parse line: {lines[i][1]!r}")
            key, rest = m.group(1), m.group(2)
            if rest is None or rest == "":
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    out[key], i = block(i + 1, lines[i + 1][0])
                else:
                    out[key], i = None, i + 1
            else:
                out[key], i = _scalar(rest), i + 1
        return out, i

    if not lines:
        return {}
    value, end = block(0, lines[0][0])
    if end != len(lines):
        raise ValueError(f"bad indentation near: {lines[end][1]!r}")
    return value


# ---------- JSON Schema subset validator (type, required, properties, additionalProperties, items, enum, pattern) ----------

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}


def validate(value, schema, path="$"):
    errors = []
    types = schema.get("type")
    if types:
        types = types if isinstance(types, list) else [types]
        ok = any(value is None if t == "null" else
                 (isinstance(value, _TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool)))
                 for t in types)
        if not ok:
            return [f"{path}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        for k, v in value.items():
            if k in props:
                errors += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown field {k}")
    if isinstance(value, list) and "items" in schema:
        for n, v in enumerate(value):
            errors += validate(v, schema["items"], f"{path}[{n}]")
    return errors


# ---------- loading ----------

def load_apps(apps_dir):
    """Return {app: {"recipe": dict, "key": dict}} for every apps/<app>/ folder."""
    apps = {}
    for d in sorted(Path(apps_dir).iterdir()):
        if (d / "answer-key.yaml").exists():
            apps[d.name] = {
                "recipe": load_yaml((d / "recipe.yaml").read_text()) if (d / "recipe.yaml").exists() else {},
                "key": load_yaml((d / "answer-key.yaml").read_text()),
            }
    return apps


def check_apps(apps, schema_dir):
    schemas = {n: json.loads((Path(schema_dir) / f"{n}.schema.json").read_text()) for n in ("recipe", "answer-key")}
    errors = []
    seen = set()
    for name, a in apps.items():
        errors += [f"{name}/recipe.yaml {e}" for e in validate(a["recipe"], schemas["recipe"])]
        errors += [f"{name}/answer-key.yaml {e}" for e in validate(a["key"], schemas["answer-key"])]
        versions = {v.get("version") for v in a["recipe"].get("versions") or []}
        for bug in a["key"].get("bugs") or []:
            if bug.get("id") in seen:
                errors.append(f"{name}: duplicate bug id {bug.get('id')}")
            seen.add(bug.get("id"))
            for tag in ("bug_tag", "fix_tag"):
                if bug.get(tag) not in versions:
                    errors.append(f"{name}: {bug.get('id')} {tag} {bug.get(tag)!r} has no entry in recipe versions")
    return errors


def index_bugs(apps):
    """bug id -> {"app", "stack" (set of tech tags), "features" (set), "split"}"""
    out = {}
    for name, a in apps.items():
        stack = {t for layer in (a["recipe"].get("stack") or {}).values() for t in (layer or [])}
        sign_in = (a["recipe"].get("sign_in") or {}).get("type")
        for bug in a["key"].get("bugs") or []:
            features = set(bug.get("feature_tags") or [])
            if sign_in:
                features.add(f"sign-in-{sign_in}")
            out[bug["id"]] = {"app": name, "stack": stack, "features": features, "split": bug.get("split") or ""}
    return out


def read_results(path):
    rows = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            row["_line"] = n
            rows.append(row)
    return rows


# ---------- scoring ----------

def item_runs(rows, warnings):
    """(bug_id, capability) -> {run_index: {"bug": verdict, "fixed": verdict}} with correctness recomputed."""
    runs = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        key = (r["capability"], r["version"])
        if key not in EXPECTED:
            warnings.append(f"line {r['_line']}: unknown capability/version {key}, skipped")
            continue
        want = EXPECTED[key]
        if r.get("expected") not in (None, want):
            warnings.append(f"line {r['_line']}: expected {r.get('expected')!r} but the rule says {want!r}; rule used")
        if "correct" in r and r["correct"] != (r["verdict"] == want):
            warnings.append(f"line {r['_line']}: correct={r['correct']} disagrees with the rule; recomputed")
        slot = runs[(r["bug_id"], r["capability"])][r.get("run_index", 0)]
        if r["version"] in slot:
            warnings.append(f"line {r['_line']}: duplicate {r['bug_id']} {key} run {r.get('run_index', 0)}; first kept")
            continue
        slot[r["version"]] = r["verdict"]
    return runs


def pair_correct(capability, pair):
    return all(pair.get(v) == EXPECTED[(capability, v)] for v in ("bug", "fixed"))


def stats(runs, bug_ids, capability, n_runs):
    """Single-run accuracy (first complete run) and consistency over the first n_runs complete runs."""
    k = n = stable = all_ok = m = 0
    for bug_id in sorted(bug_ids):
        by_run = runs.get((bug_id, capability))
        if not by_run:
            continue
        complete = [by_run[i] for i in sorted(by_run) if {"bug", "fixed"} <= set(by_run[i])]
        if not complete:
            continue
        n += 1
        k += pair_correct(capability, complete[0])
        if len(complete) >= n_runs:
            first = complete[:n_runs]
            m += 1
            stable += all((p["bug"], p["fixed"]) == (first[0]["bug"], first[0]["fixed"]) for p in first)
            all_ok += all(pair_correct(capability, p) for p in first)
    return {"k": k, "n": n, "stable": stable, "pass_all": all_ok, "m": m}


def headline(cap_stats):
    """Weighted mean of Wilson lower bounds; unmeasured capabilities count 0."""
    total, measured = 0.0, []
    for cap in CAPABILITIES:
        s = cap_stats[cap]
        if s["n"]:
            total += WEIGHTS[cap] * wilson(s["k"], s["n"])[0]
            measured.append(cap)
    k = sum(cap_stats[c]["pass_all"] for c in CAPABILITIES)
    m = sum(cap_stats[c]["m"] for c in CAPABILITIES)
    if m:
        total += WEIGHTS["consistency"] * wilson(k, m)[0]
        measured.append("consistency")
    stable = sum(cap_stats[c]["stable"] for c in CAPABILITIES)
    return total, measured, (stable / m if m else None)


def groups(bugs):
    out = defaultdict(set)
    for bug_id, b in bugs.items():
        out[("all", "all")].add(bug_id)
        out[("app", b["app"])].add(bug_id)
        for t in b["stack"]:
            out[("tech", t)].add(bug_id)
        for f in b["features"]:
            out[("feature", f)].add(bug_id)
    return out


def score(rows, apps, split="heldout", n_runs=5):
    """Return (report lines, warnings). Raises ValueError when results mix stamps."""
    warnings = []
    stamps = {(r.get("model"), r.get("contextqa_version")) for r in rows}
    if len(stamps) > 1:
        raise ValueError(f"results mix {len(stamps)} model/version stamps {sorted(map(str, stamps))}; score one at a time")
    bugs = index_bugs(apps)
    unknown = sorted({r["bug_id"] for r in rows} - set(bugs))
    if unknown:
        warnings.append(f"results name bugs with no answer key, skipped: {unknown}")
    if split != "all":
        unsplit = [b for b in bugs if not bugs[b]["split"]]
        if unsplit:
            warnings.append(f"{len(unsplit)} bugs have no split yet and are excluded from split={split}")
        bugs = {b: v for b, v in bugs.items() if v["split"] == split}
    rows = [r for r in rows if r["bug_id"] in bugs]
    runs = item_runs(rows, warnings)
    for (bug_id, cap), by_run in sorted(runs.items()):
        missing = [i for i in sorted(by_run) if len(by_run[i]) < 2]
        if missing:
            warnings.append(f"{bug_id} {cap}: runs {missing} miss the bug or fixed half, not scored")

    dates = sorted(r.get("started_at", "")[:10] for r in rows if r.get("started_at"))
    model, version = next(iter(stamps)) if stamps else (None, None)
    out = [f"stamp: model={model} contextqa={version} dates={dates[0] if dates else '-'}..{dates[-1] if dates else '-'}"
           f" split={split} consistency N={n_runs}", ""]
    header = f"{'group':<28} {'capability':<11} {'score (Wilson 95%)':<30} {'stable':<8} {'pass^' + str(n_runs):<8} band"
    for kind in ("all", "app", "tech", "feature"):
        out += [f"== per {kind} ==", header]
        for (gk, gname), ids in sorted(groups(bugs).items()):
            if gk != kind:
                continue
            cap_stats = {c: stats(runs, ids, c, n_runs) for c in CAPABILITIES}
            for cap in CAPABILITIES:
                s = cap_stats[cap]
                ci = wilson(s["k"], s["n"])
                stable = s["stable"] / s["m"] if s["m"] else None
                cons = (f"{s['stable']}/{s['m']}", f"{s['pass_all']}/{s['m']}") if s["m"] else ("-", "-")
                out.append(f"{gname:<28} {cap:<11} {fmt_ci(s['k'], s['n']):<30} {cons[0]:<8} {cons[1]:<8} "
                           f"{band(ci[0] if ci else None, stable)}")
            total, measured, stable = headline(cap_stats)
            all_measured = set(measured) == set(WEIGHTS)
            out.append(f"{'':<28} {'HEADLINE':<11} ≥ {pct_floor(total)}% weighted, {len(measured)}/{len(WEIGHTS)} "
                       f"capabilities measured -> {band(total if measured else None, stable, all_measured)}")
        out.append("")
    return out, sorted(set(warnings))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results", nargs="?", help="results.jsonl")
    p.add_argument("--apps", default=str(ROOT / "apps"))
    p.add_argument("--schema", default=str(ROOT / "schema"))
    p.add_argument("--split", default="heldout", choices=["heldout", "dev", "all"])
    p.add_argument("--n", type=int, default=5, help="runs per item for consistency (default 5; 3 is the minimum)")
    p.add_argument("--check", action="store_true", help="validate answer keys and recipes, then exit")
    a = p.parse_args(argv)
    apps = load_apps(a.apps)
    errors = check_apps(apps, a.schema)
    if errors or a.check:
        print("\n".join(errors) or f"ok: {len(apps)} apps, {len(index_bugs(apps))} bugs")
        return 1 if errors else 0
    if not a.results:
        p.error("give a results.jsonl file, or --check")
    try:
        lines, warnings = score(read_results(a.results), apps, a.split, a.n)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
