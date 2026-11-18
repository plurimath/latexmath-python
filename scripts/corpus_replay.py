"""Replay one pinned plurimath-testsuite latex generation on the installed wheel.

The pin (ci/testsuite-pin.yml, plurimath-stage/pin/1) is the one the Ruby gem
plurimath/latexmath replays. ci/testsuite-replay.json is this consumer's own
record of what it executes: the row count per boundary and the SHA-256 of the
sorted executed IDs. Every check in docs/stage-corpus-contract.adoc runs
before any row does: checkout at the pinned commit, the named ledger entry,
the recomputed snapshot digest, provenance bytes/SHA-256/row IDs for every
payload (nothing unlisted), unique row IDs and the ledger counts.

Boundaries:

* ``raw-tree`` rows run through ``latexmath.parse_raw``: the preprocessed text
  and the whole raw tree must be equal, or the refusal byte offset on the
  caller basis (``ParseError.byte_offset``) must be equal.
* ``typed-model`` rows run through ``latexmath.parse_model_json``: a refusal
  row must be refused with ``ModelParseError``; its offset is the caller byte
  offset, read from ``parse_raw`` because the model parse reports preprocessed
  offsets. A model row must equal the expected model as JSON values. The
  derived ``MathSymbol`` members ``value`` and ``kind`` are left out of the
  comparison when the row's object does not record them (rows were written
  before the model declared them), as the producer's replay does.
* Legacy-oracle payloads (target renderings of the old facade) are counted and
  not executed: this package has no such output.

Nothing is skipped: every row of an executed boundary runs, and the set of
executed IDs must equal the one recorded in ci/testsuite-replay.json.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "ci/testsuite-pin.yml"
EXPECTATION = ROOT / "ci/testsuite-replay.json"
REPOSITORY = "https://github.com/plurimath/plurimath-testsuite"
EXECUTED = ("raw-tree", "typed-model")


class ReplayError(RuntimeError):
    """The pinned generation failed verification or replay."""


def _yaml():
    import yaml
    loader = getattr(yaml, "CSafeLoader", None)
    if loader is None:
        raise ReplayError("PyYAML without libyaml: the generation must be read with CSafeLoader")
    return lambda data: yaml.load(data, Loader=loader)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_pin(path=PIN) -> dict:
    pin = _yaml()(Path(path).read_bytes())
    required = {"schema", "repository", "commit", "language", "generation", "snapshot_digest"}
    if not isinstance(pin, dict) or set(pin) != required:
        raise ReplayError("pin must carry exactly the plurimath-stage/pin/1 keys")
    if pin["schema"] != "plurimath-stage/pin/1" or pin["repository"] != REPOSITORY:
        raise ReplayError("pin schema or repository differs from plurimath-stage/pin/1")
    if not re.fullmatch(r"[0-9a-f]{40}", str(pin["commit"])):
        raise ReplayError("pin commit must be a full 40-hex SHA")
    if pin["language"] != "latex":
        raise ReplayError("this consumer replays the latex language only")
    if not isinstance(pin["generation"], int) or isinstance(pin["generation"], bool) or pin["generation"] < 1:
        raise ReplayError("pin generation must be a positive integer")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(pin["snapshot_digest"])):
        raise ReplayError("pin snapshot_digest must be sha256:<64 hex>")
    return pin


def checkout(pin: dict, destination: Path) -> Path:
    """Fetch exactly the pinned commit into a fresh directory."""
    destination = Path(destination)
    if destination.exists():
        raise ReplayError(f"{destination} already exists; replay needs a fresh checkout")
    git = lambda *args: subprocess.run(["git", *args], cwd=destination, check=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    destination.mkdir(parents=True)
    git("init", "-q")
    git("config", "core.autocrlf", "false")
    git("remote", "add", "origin", pin["repository"] + ".git")
    git("fetch", "-q", "--depth", "1", "origin", pin["commit"])
    git("checkout", "-q", "--detach", "FETCH_HEAD")
    return destination


def _safe(relative: str) -> str:
    parts = relative.split("/")
    if PurePosixPath(relative).is_absolute() or any(part in ("", ".", "..") for part in parts) or "\\" in relative:
        raise ReplayError(f"unsafe path in generation: {relative!r}")
    return relative


def snapshot_digest(directory: Path) -> tuple[str, dict]:
    """sha256 over '<sha256>  <relative path>\\n' lines, ordered by path bytes."""
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ReplayError(f"symlink in generation: {path}")
        if path.is_file():
            relative = _safe(path.relative_to(directory).as_posix())
            files[relative] = path.read_bytes()
    if not files:
        raise ReplayError("empty generation")
    lines = "".join(f"{sha256(files[name])}  {name}\n" for name in sorted(files, key=lambda name: name.encode("utf-8")))
    return "sha256:" + sha256(lines.encode("utf-8")), files


def verify(checkout_root: Path, pin: dict) -> dict:
    """Run every contract check before any row is replayed."""
    load = _yaml()
    root = Path(checkout_root)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()
    if head != pin["commit"]:
        raise ReplayError(f"checkout is at {head}, pin names {pin['commit']}")
    ledger = load((root / "generations" / (pin["language"] + ".yaml")).read_bytes())
    if ledger.get("schema") != "plurimath-stage/ledger/1" or ledger.get("language") != pin["language"]:
        raise ReplayError("ledger schema or language differs")
    entries = [entry for entry in ledger["entries"] if entry.get("generation") == pin["generation"]]
    if len(entries) != 1:
        raise ReplayError(f"ledger has {len(entries)} entries for generation {pin['generation']}")
    entry = entries[0]
    if entry["snapshot_digest"] != pin["snapshot_digest"]:
        raise ReplayError("ledger snapshot digest differs from the pin")
    expected_path = f"stages/{pin['language']}/generations/{pin['generation']:06d}"
    if entry["path"] != expected_path:
        raise ReplayError(f"ledger path {entry['path']} is not {expected_path}")
    directory = root / _safe(entry["path"])
    digest, files = snapshot_digest(directory)
    if digest != pin["snapshot_digest"]:
        raise ReplayError(f"recomputed snapshot digest {digest} differs from the pin")
    provenance = load(files["provenance.yaml"])
    if (provenance.get("language"), provenance.get("generation")) != (pin["language"], pin["generation"]):
        raise ReplayError("provenance language or generation differs from the pin")
    listed = [_safe(payload["path"]) for payload in provenance["payloads"]]
    if len(set(listed)) != len(listed):
        raise ReplayError("provenance lists a payload twice")
    if set(listed) != set(files) - {"provenance.yaml"}:
        raise ReplayError("payload files on disk differ from provenance (missing or unlisted)")
    seen, documents = set(), []
    for payload in provenance["payloads"]:
        data = files[payload["path"]]
        if len(data) != payload["bytes"] or sha256(data) != payload["sha256"]:
            raise ReplayError(f"{payload['path']} bytes or SHA-256 differ from provenance")
        document = load(data)
        ids = [case["id"] for case in document["cases"]]
        if ids != list(payload["rows"]):
            raise ReplayError(f"{payload['path']} row IDs differ from provenance")
        if seen & set(ids) or len(set(ids)) != len(ids):
            raise ReplayError(f"{payload['path']} repeats a row ID")
        seen.update(ids)
        documents.append((payload["path"], document))
    if len(documents) != entry["payloads"] or len(seen) != entry["rows"]:
        raise ReplayError("payload or row count differs from the ledger entry")
    return {"head": head, "entry": entry, "documents": documents, "provenance_sha256": sha256(files["provenance.yaml"])}


def _raw(stage, case):
    source, decimal = case["input"]["str"], case.get("options", {}).get("decimal", ".")
    expected = case["expected"]
    if set(expected) == {"refusal"}:
        try:
            stage.parse_raw(source, decimal=decimal)
        except stage.ParseError as error:
            want = expected["refusal"]["byte_offset"]
            if error.byte_offset != want:
                return f"refusal at caller byte {error.byte_offset}, expected {want}"
            return None
        return "accepted, expected a refusal"
    if set(expected) != {"preprocessed", "tree"}:
        raise ReplayError(f"{case['id']}: unknown raw-tree expectation keys {sorted(expected)}")
    try:
        result = stage.parse_raw(source, decimal=decimal)
    except stage.ParseError as error:
        return f"refused at caller byte {error.byte_offset}, expected a tree"
    if result.text != expected["preprocessed"]["str"]:
        return "preprocessed text differs"
    if result.tree != _decode(expected["tree"], result.text):
        return "raw tree differs"
    return None


def _decode(node, text):
    """The corpus wire form of a raw tree as the Python values of ``RawParseResult.tree``."""
    if "str" in node:
        return node["str"]
    if "sym" in node:
        return node["sym"]
    if "slice" in node:
        value, offset = node["slice"], node["byte_offset"]
        prefix = text.encode("utf-8")[:offset].decode("utf-8")
        if len(prefix) != node["char_offset"]:
            raise ReplayError("slice char_offset disagrees with byte_offset")
        return {"offset": offset, "length": len(value), "line": prefix.count("\n") + 1,
                "column": len(prefix.rsplit("\n", 1)[-1]) + 1, "value": value}
    if "array" in node:
        return [_decode(item, text) for item in node["array"]]
    if "hash" in node:
        pairs = [(_decode(key, text), _decode(value, text)) for key, value in node["hash"]]
        if len({key for key, _ in pairs}) != len(pairs):
            raise ReplayError("duplicate key in expected hash")
        return dict(pairs)
    if "nil" in node:
        return None
    if "bool" in node:
        return node["bool"]
    if "int" in node:
        return int(node["int"])
    raise ReplayError(f"unknown source tag {sorted(node)}")


def _model_value(value):
    """One typed-model expectation value as portable JSON: {node, fields}
    becomes an object of its fields (the Ruby class name is dropped), {array} a
    list, {str} a string."""
    if set(value) == {"str"}:
        return value["str"]
    if set(value) == {"array"}:
        return [_model_value(item) for item in value["array"]]
    if set(value) == {"node", "fields"}:
        if not str(value["node"]).startswith("Latexmath::Model::"):
            raise ReplayError(f"unexpected model class {value['node']}")
        names = [name for name, _ in value["fields"]]
        if len(set(names)) != len(names):
            raise ReplayError("duplicate model field")
        return {name: _model_value(item) for name, item in value["fields"]}
    raise ReplayError(f"unknown typed-model value keys {sorted(value)}")


def _project(actual, expected):
    """``actual`` without the derived MathSymbol members ``value`` and ``kind``
    that the matching expected object does not record. Nothing else is dropped."""
    if isinstance(actual, dict) and isinstance(expected, dict):
        symbol = "spelling" in actual
        return {key: _project(item, expected.get(key))
                for key, item in actual.items()
                if not (symbol and key in ("value", "kind") and key not in expected)}
    if isinstance(actual, list) and isinstance(expected, list):
        return [_project(item, expected[i] if i < len(expected) else None) for i, item in enumerate(actual)]
    return actual


def _model_difference(actual, expected, path="$"):
    """The first path where two JSON values differ, or None."""
    if isinstance(expected, dict) != isinstance(actual, dict) or isinstance(expected, list) != isinstance(actual, list):
        return f"{path}: actual {type(actual).__name__}, expected {type(expected).__name__}"
    if isinstance(expected, dict):
        if set(actual) != set(expected):
            return f"{path}: actual fields {sorted(actual)}, expected fields {sorted(expected)}"
        for name in expected:
            found = _model_difference(actual[name], expected[name], f"{path}.{name}")
            if found:
                return found
        return None
    if isinstance(expected, list):
        if len(actual) != len(expected):
            return f"{path}: actual {len(actual)} items, expected {len(expected)}"
        for index, (left, right) in enumerate(zip(actual, expected)):
            found = _model_difference(left, right, f"{path}[{index}]")
            if found:
                return found
        return None
    return None if type(actual) is type(expected) and actual == expected else f"{path}: actual {actual!r}, expected {expected!r}"


def _model(stage, case):
    source, decimal = case["input"]["str"], case.get("options", {}).get("decimal", ".")
    expected = case["expected"]
    if set(expected) not in ({"refusal"}, {"preprocessed", "tree"}):
        raise ReplayError(f"{case['id']}: unknown typed-model expectation keys {sorted(expected)}")
    try:
        actual = stage.parse_model_json(source, decimal=decimal)
    except stage.ModelParseError:
        if "refusal" not in expected:
            return "refused, expected a model"
        want = expected["refusal"].get("byte_offset")
        try:
            stage.parse_raw(source, decimal=decimal)
        except stage.ParseError as error:
            if want is not None and error.byte_offset != want:
                return f"refusal at caller byte {error.byte_offset}, expected {want}"
            return None
        return "refused by parse_model_json, accepted by parse_raw"
    if "refusal" in expected:
        return "accepted, expected a refusal"
    want = _model_value(expected["tree"])
    found = _model_difference(_project(actual, want), want)
    return f"model content differs at {found}" if found else None


_STAGE = None


def _stage():
    """Import the installed wheel once per process, with the replay's limits."""
    global _STAGE
    if _STAGE is None:
        import latexmath as stage
        if Path(stage.__file__).resolve().is_relative_to(ROOT / "src"):
            raise ReplayError("replay must exercise an installed wheel, not the source checkout")
        # Raw trees nest deeply; _decode and the comparisons recurse per level.
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))
        _STAGE = stage
    return _STAGE


def _row(task):
    """Replay one row; returns the problem text or None."""
    boundary, case = task
    stage = _stage()
    return _raw(stage, case) if boundary == "raw-tree" else _model(stage, case)


def _jobs() -> int:
    """Worker processes. Rows are independent, so they run on every core. LATEXMATH_REPLAY_JOBS overrides."""
    value = os.environ.get("LATEXMATH_REPLAY_JOBS")
    return max(1, int(value) if value else (os.cpu_count() or 1))


def replay(verified: dict) -> dict:
    _stage()
    executed = {boundary: [] for boundary in EXECUTED}
    counted, tasks, origins = {}, [], []
    for path, document in verified["documents"]:
        boundary = document.get("boundary")
        if boundary not in EXECUTED:
            key = boundary or document.get("schema")
            counted[key] = counted.get(key, 0) + len(document["cases"])
            continue
        for case in document["cases"]:
            tasks.append((boundary, case))
            origins.append((path, boundary, case["id"]))
    jobs = min(_jobs(), max(1, len(tasks)))
    if jobs == 1:
        problems = [_row(task) for task in tasks]
    else:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=jobs, initializer=_stage) as pool:
            problems = list(pool.map(_row, tasks, chunksize=4))
    failures = []
    for (path, boundary, identifier), problem in zip(origins, problems, strict=True):
        executed[boundary].append(identifier)
        if problem:
            failures.append({"id": identifier, "payload": path, "boundary": boundary, "problem": problem})
    return {
        "executed_ids": sorted(executed["raw-tree"] + executed["typed-model"]),
        "executed": {boundary: {"rows": len(ids), "ids_sha256": sha256("".join(i + "\n" for i in sorted(ids)).encode("utf-8"))}
                     for boundary, ids in executed.items()},
        "not_executed": counted,
        "failures": failures,
        "jobs": jobs,
    }


def run(pin_path=PIN, expectation_path=EXPECTATION, workdir=None, evidence=None) -> dict:
    pin = load_pin(pin_path)
    expectation = json.loads(Path(expectation_path).read_text(encoding="utf-8"))
    if {key: expectation.get(key) for key in ("commit", "generation", "snapshot_digest")} != \
            {key: pin[key] for key in ("commit", "generation", "snapshot_digest")}:
        raise ReplayError("ci/testsuite-replay.json describes a different pin")
    workdir = Path(workdir or ROOT / ".testsuite")
    verified = verify(checkout(pin, workdir), pin)
    report = {"pin": pin, "provenance_sha256": verified["provenance_sha256"], **replay(verified)}
    if evidence:
        # One ID per line.
        Path(evidence).with_name("executed-ids.txt").write_text(
            "".join(i + "\n" for i in report["executed_ids"]), encoding="utf-8")
        Path(evidence).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {"jobs": report["jobs"], "executed": report["executed"], "not_executed": report["not_executed"], "failures": len(report["failures"])}
    print(json.dumps(summary, indent=2))
    for failure in report["failures"][:40]:
        print(f"FAIL {failure['id']} [{failure['boundary']}] {failure['problem']}")
    if report["failures"]:
        raise ReplayError(f"{len(report['failures'])} pinned rows disagree with the installed wheel")
    if report["executed"] != expectation["executed"]:
        raise ReplayError("executed rows or IDs differ from ci/testsuite-replay.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pin", default=PIN)
    parser.add_argument("--expectation", default=EXPECTATION)
    parser.add_argument("--workdir")
    parser.add_argument("--evidence", default=ROOT / ".ci-evidence/corpus-replay.json")
    args = parser.parse_args()
    Path(args.evidence).parent.mkdir(exist_ok=True)
    run(args.pin, args.expectation, args.workdir, args.evidence)


if __name__ == "__main__":
    main()
