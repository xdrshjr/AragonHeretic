"""Offline evidence, artifact and PDF verification for the ARA manuscript."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

sys.dont_write_bytecode = True
REPO_ROOT = Path(__file__).resolve().parents[4]
PAPER_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "docs/logs/ara-v1"
SPEC_PATH = REPO_ROOT / "docs/plans/heretic-ara-manuscript/spec.md"
TITLE = "面向语言模型拒答行为干预的校准式任意秩消融"
LABELS = {
    "sec:abstract", "sec:introduction", "sec:background", "sec:method",
    "sec:adaptation", "sec:experiments", "sec:discussion", "sec:conclusion",
    "tab:results", "tab:distribution", "tab:protocol", "tab:methods",
    "fig:architecture", "fig:search",
}
CLAIM_FIELDS = {
    "claim_id", "section_label", "claim_text", "evidence_kind", "source_id",
    "source_path", "locator", "limitations",
}


class VerificationError(ValueError):
    """A required evidence or artifact invariant did not hold."""


def _require(condition, message):
    if not condition:
        raise VerificationError(message)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_constant(value):
    raise VerificationError(f"Non-finite JSON constant: {value}")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"),
                      parse_constant=_reject_constant)


def _write_json(path, value):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    staged = path.with_suffix(path.suffix + ".tmp")
    staged.write_text(encoded + "\n", encoding="utf-8", newline="\n")
    staged.replace(path)


def _metadata(path):
    match = re.search(r"```json\s*\n(.*?)\n```",
                      path.read_text(encoding="utf-8"), re.S)
    _require(match is not None, f"Missing first JSON block: {path}")
    result = json.loads(match[1], parse_constant=_reject_constant)
    _require(isinstance(result, dict), f"JSON object required: {path}")
    return result


def _workspace_path(path):
    resolved = Path(path).resolve()
    _require(resolved.is_relative_to(REPO_ROOT),
             f"Path escapes workspace: {resolved}")
    return resolved


def _paper_path(paper_dir, relative):
    path = _workspace_path(paper_dir / relative)
    _require(path.is_relative_to(paper_dir.resolve()),
             f"Path escapes paper directory: {path}")
    return path


def _tool_roots(explicit=None):
    roots = [Path(explicit)] if explicit else []
    for name in ("xelatex", "pdftotext"):
        found = shutil.which(name)
        if found:
            roots.append(Path(found).parent)
    local = Path(os.environ.get("LOCALAPPDATA", "C:/Users/Administrator"))
    roots += [local / "Programs/MiKTeX/miktex/bin/x64"]
    roots += [Path("C:/Program Files/MiKTeX/miktex/bin/x64")]
    roots += sorted(Path("C:/texlive").glob("*/bin/windows"), reverse=True)
    roots += sorted(Path("C:/texlive").glob("*/bin/win32"), reverse=True)
    return roots


def discover_tools(explicit=None):
    """Find a complete existing TeX tool suite without executing installers.

    Args:
        explicit: Optional preferred binary directory.
    Returns:
        A mapping of executable names to absolute paths.
    Raises:
        VerificationError: A complete suite cannot be found.
    """
    names = ("xelatex", "bibtex", "pdftotext", "pdftoppm", "kpsewhich")
    suffix = ".exe" if os.name == "nt" else ""
    for root in _tool_roots(explicit):
        found = {name: str(root / (name + suffix)) for name in names}
        if all(Path(path).is_file() for path in found.values()):
            return _pdf_tool_overrides(found)
        if explicit and root == Path(explicit):
            missing = [name for name, path in found.items()
                       if not Path(path).is_file()]
            raise VerificationError(f"Missing in TexBin {root}: {missing}")
    raise VerificationError("TeX tools missing; supply an installed -TexBin")


def _pdf_tool_overrides(found):
    fallback = PAPER_DIR / "build/xpdf"
    names = ("pdftotext", "pdftoppm")
    found["pdf_family"] = "poppler"
    if all((fallback / (name + ".exe")).is_file() for name in names):
        found.update({name: str(fallback / (name + ".exe")) for name in names})
        found["pdf_family"] = "xpdf"
    return found


def _miktex_environment(build, binary_dir):
    prefix = build / "miktex"
    install = binary_dir.parents[2]
    roots = {
        "MIKTEX_USERCONFIG": prefix / "config",
        "MIKTEX_USERDATA": prefix / "data",
        "MIKTEX_COMMONCONFIG": prefix / "common-config",
        "MIKTEX_COMMONDATA": prefix / "common-data",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    config = roots["MIKTEX_USERCONFIG"] / "miktex/config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "miktex.ini").write_text(
        "[MPM]\nAutoInstall=0\n[Core]\nAutoAdmin=f\n"
        "ShellCommandMode=Forbidden\n", encoding="utf-8")
    roots["MIKTEX_USERINSTALL"] = install
    roots["MIKTEX_COMMONINSTALL"] = install
    roots["MIKTEX_USERLOGDIRECTORY"] = build / "logs"
    roots["MIKTEX_COMMONLOGDIRECTORY"] = build / "logs"
    _copy_existing_format(roots["MIKTEX_USERDATA"])
    return {key: str(value) for key, value in roots.items()}


def _copy_existing_format(destination):
    local = Path(os.environ.get("LOCALAPPDATA", "C:/Users/Administrator"))
    existing = local / "MiKTeX/miktex/data/le/xetex/xelatex.fmt"
    target = destination / "miktex/data/le/xetex/xelatex.fmt"
    if existing.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(existing, target)


def prepare_environment(paper_dir, tools=None):
    """Prepare writable caches within the paper workspace, without installs.

    Args:
        paper_dir: Paper directory inside the repository.
        tools: Optional executable mapping for TeX configuration.
    Returns:
        Environment overrides for subprocesses.
    Raises:
        VerificationError: A cache path escapes the workspace.
    """
    build = _paper_path(paper_dir, "build")
    names = {"TEMP": "tmp", "TMP": "tmp", "TMPDIR": "tmp",
             "MPLCONFIGDIR": "matplotlib", "XDG_CACHE_HOME": "cache",
             "TEXMFVAR": "texlive/var", "TEXMFCONFIG": "texlive/config"}
    environment = {key: str(build / name) for key, name in names.items()}
    for path in [*environment.values(), str(build / "logs")]:
        _workspace_path(path).mkdir(parents=True, exist_ok=True)
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    environment["TEXINPUTS"] = str(build / "texmf/tex") + "//" + os.pathsep
    if tools:
        binary_dir = Path(tools["xelatex"]).parent
        environment["PATH"] = str(binary_dir) + os.pathsep + os.environ["PATH"]
        if "miktex" in str(binary_dir).lower():
            environment.update(_miktex_environment(build, binary_dir))
        environment.update(_font_environment(build, binary_dir))
        if tools.get("pdf_family") == "xpdf":
            _prepare_xpdf_config(build, binary_dir.parents[2])
    return environment


def _prepare_xpdf_config(build, install):
    mapping = install / "poppler"
    lines = []
    collections = ("Adobe-GB1", "Adobe-CNS1", "Adobe-Japan1", "Adobe-Korea1")
    for collection in collections:
        unicode_path = mapping / "cidToUnicode" / collection
        if unicode_path.is_file():
            filename = unicode_path.as_posix()
            lines.append(f'cidToUnicode {collection} "{filename}"')
            directory = (mapping / "cMap" / collection).as_posix()
            lines.append(f'cMapDir {collection} "{directory}"')
    _require(bool(lines), "Xpdf needs installed Poppler character mapping data")
    (build / "xpdfrc").write_text("\n".join(lines) + "\n", encoding="utf-8")


def convert_rendered_pages(paper_dir):
    """Convert Xpdf's lossless PPM renderings to 120 dpi PNG artifacts.

    Args:
        paper_dir: Paper directory containing rendered PPM pages.
    Returns:
        Number of pages converted without resizing.
    Raises:
        OSError: A rendered page cannot be decoded or saved.
    """
    from PIL import Image
    count = 0
    for path in sorted((paper_dir / "build/pages").glob("page-*.ppm")):
        with Image.open(path) as page:
            page.save(path.with_suffix(".png"), dpi=(120, 120))
        path.unlink()
        count += 1
    return count


def _font_environment(build, binary_dir):
    install = binary_dir.parents[2]
    font_dirs = [install / "fonts", Path("C:/Windows/Fonts")]
    config = build / "fontconfig/fonts.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    cache = build / "fontconfig/cache"
    cache.mkdir(parents=True, exist_ok=True)
    content = ["<?xml version='1.0'?><fontconfig>"]
    content += [f"<dir>{escape(path.as_posix())}</dir>" for path in font_dirs]
    content += [f"<cachedir>{escape(cache.as_posix())}</cachedir>",
                "</fontconfig>"]
    config.write_text("\n".join(content), encoding="utf-8")
    return {"FONTCONFIG_FILE": str(config),
            "FONTCONFIG_PATH": str(config.parent)}


def _read_source(record):
    if record["source_kind"] == "git_blob":
        from ara_v1_data import read_git_blob
        return read_git_blob(record["revision"], record["path"])
    return _workspace_path(REPO_ROOT / record["path"]).read_bytes()


def _symbols(content):
    parsed = ast.parse(content.decode("utf-8-sig"))
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {node.name for node in ast.walk(parsed) if isinstance(node, kinds)}


def _source_provenance(paper_dir, summary):
    document = _metadata(paper_dir / "evidence/source-provenance.md")
    entries = document["sources"]
    indexed = {entry["source_id"]: entry for entry in entries}
    records = summary["source_records"]
    _require(len(indexed) == len(entries), "Duplicate provenance source_id")
    _require(set(indexed) == {item["source_id"] for item in records},
             "Provenance sources do not match summary.source_records")
    for record in records:
        entry = indexed[record["source_id"]]
        for key in ("source_kind", "path", "revision", "sha256"):
            _require(entry.get(key) == record[key],
                     f"Provenance mismatch {record['source_id']}: {key}")
        content = _read_source(record)
        _require(hashlib.sha256(content).hexdigest() == record["sha256"],
                 f"Changed source bytes: {record['source_id']}")
        requested = set(entry.get("symbols", []))
        if requested:
            _require(requested <= _symbols(content),
                     f"Missing source symbols: {record['source_id']}")
    return {entry["source_id"]: entry for entry in records}


def _tex_inputs(paper_dir):
    def visit(relative, active):
        path = _paper_path(paper_dir, relative)
        if not path.suffix:
            path = path.with_suffix(".tex")
        _require(path.is_file(), f"Missing TeX input: {relative}")
        _require(path not in active, f"Cyclic TeX input: {relative}")
        content = path.read_text(encoding="utf-8")
        content = re.sub(r"(?<!\\)%[^\n]*", "", content)
        yield path, content
        for name in re.findall(r"\\(?:input|include)\{([^}]+)\}", content):
            yield from visit(name, active | {path})

    # Preserve repeated inclusions: TeX would emit their labels twice too.
    return list(visit("paper.tex", set()))


def _tex_document(paper_dir):
    content = "\n".join(text for _, text in _tex_inputs(paper_dir))
    labels = re.findall(r"\\label\{([^}]+)\}", content)
    _require(len(labels) == len(set(labels)), "Duplicate TeX labels")
    _require(LABELS <= set(labels), f"Missing labels: {LABELS - set(labels)}")
    references = set(re.findall(r"\\(?:ref|eqref|pageref)\{([^}]+)\}", content))
    _require(references <= set(labels),
             f"Undefined labels: {references - set(labels)}")
    return content, set(labels)


def _literature(paper_dir, content):
    bib = (paper_dir / "references.bib").read_text(encoding="utf-8")
    keys = re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", bib)
    _require(len(keys) == len(set(keys)), "Duplicate BibTeX keys")
    cited = set()
    for group in re.findall(r"\\cite\w*\*?(?:\[[^]]*\])*\{([^}]+)\}", content):
        cited.update(key.strip() for key in group.split(","))
    _require(cited <= set(keys), f"Undefined citations: {cited - set(keys)}")
    entries = _metadata(paper_dir / "evidence/literature-provenance.md")
    entries = entries["references"]
    _require({entry["bibkey"] for entry in entries} == set(keys),
             "Literature metadata must cover exactly the bibliography")
    indexed = {entry["source_id"]: entry for entry in entries}
    _require(len(indexed) == len(entries), "Duplicate literature source_id")
    for entry in entries:
        _check_bib_fields(bib, entry)
        _require(entry.get("verified") is True, "Unverified literature entry")
        fields = ["url", "title", "year", "accessed_at"]
        if entry.get("source_kind") != "local_artifact":
            fields.append("authors")
        for key in fields:
            _require(bool(entry.get(key)), f"Literature field missing: {key}")
        if entry.get("source_kind") == "local_artifact":
            _require(_workspace_path(REPO_ROOT / entry["url"]).is_file(),
                     "Local bibliography artifact missing")
        else:
            _require(entry["url"].startswith("https://"),
                     "Original HTTPS URL required")
    return indexed


def _check_bib_fields(bib, entry):
    marker = re.search(r"@\w+\s*\{\s*" + re.escape(entry["bibkey"])
                       + r"\s*,", bib)
    stop = re.search(r"\n@", bib[marker.end():])
    end = marker.end() + stop.start() if stop else len(bib)
    block = bib[marker.end():end]
    title = re.search(r"\btitle\s*=\s*\{(.*?)\}\s*,\s*\n", block, re.S)
    year = re.search(r"\byear\s*=\s*\{(\d+)\}", block)
    _require(title is not None and year is not None,
             f"Braced title/year required: {entry['bibkey']}")
    def clean(value):
        return re.sub(r"[{}\s]", "", value).casefold()

    _require(clean(title[1]) == clean(entry["title"]),
             f"BibTeX title differs from provenance: {entry['bibkey']}")
    _require(int(year[1]) == entry["year"],
             f"BibTeX year differs from provenance: {entry['bibkey']}")


def _summary_locator(summary, locator):
    current = summary
    tokens = re.findall(r"[^.\[\]]+", locator)
    _require(bool(tokens), "Empty summary locator")
    for token in tokens:
        key = int(token) if isinstance(current, list) else token
        current = current[key]
    return current


def _local_locator(claim, record, summary):
    locator = claim["locator"]
    prefix, _, target = locator.partition(":")
    content = _read_source(record)
    if prefix == "summary":
        _summary_locator(summary, target)
    elif prefix == "symbol":
        _require(target in _symbols(content), f"Missing symbol: {locator}")
    elif prefix in ("line", "lines"):
        values = [int(value) for value in target.split("-")]
        _require(1 <= len(values) <= 2, f"Invalid lines: {locator}")
        _require(1 <= min(values) <= max(values) <= len(content.split(b"\n")),
                 f"Lines outside source: {locator}")
    else:
        raise VerificationError(f"Unsupported local locator: {locator}")
    is_python = record["path"].endswith(".py")
    if claim["evidence_kind"] == "implemented" and is_python:
        _require(prefix == "symbol",
                 "Implemented Python claims require symbol locators")


def _claims(paper_dir, context):
    summary, sources, literature, labels = context
    path = paper_dir / "evidence/claim-traceability.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        _require(set(reader.fieldnames or []) == CLAIM_FIELDS,
                 "Claim CSV fields differ from approved schema")
        entries = list(reader)
    _require(bool(entries), "Claim table is empty")
    for claim in entries:
        _require(claim["section_label"] in labels,
                 f"Claim label missing: {claim['claim_id']}")
        _require(bool(claim["claim_id"] and claim["claim_text"]), "Empty claim")
        _check_claim(claim, (summary, sources, literature))
    return len(entries)


def _check_claim(claim, context):
    summary, sources, literature = context
    kind = claim["evidence_kind"]
    kinds = {"measured", "implemented", "external", "inferred", "missing"}
    _require(kind in kinds,
             f"Invalid claim kind: {kind}")
    source_id = claim["source_id"]
    if source_id in literature:
        _require(kind in ("external", "inferred"),
                 "Literature source requires external/inferred evidence")
        entry = literature[source_id]
        _require(claim["locator"] == "external:" + entry["bibkey"],
                 f"Bad external locator: {claim['claim_id']}")
        return
    _require(kind != "external", "External evidence requires literature ID")
    _require(source_id in sources, f"Unknown claim source: {source_id}")
    record = sources[source_id]
    _require(claim["source_path"] == record["path"],
             "Claim source path mismatch")
    if kind == "missing":
        _require(bool(claim["limitations"]),
                 "Missing evidence needs limitations")
        _require(claim["locator"].startswith("missing:"),
                 "Missing evidence requires an explicit missing: locator")
        return
    _local_locator(claim, record, summary)


def check_architecture(paper_dir):
    """Verify AI image identity, recorded generation and review metadata.

    Args:
        paper_dir: Paper directory.
    Returns:
        Parsed image generation metadata.
    Raises:
        VerificationError: Image bytes or generation evidence do not match.
    """
    metadata = _metadata(paper_dir / "evidence/architecture-generation.md")
    path = paper_dir / "figures/ara-architecture.png"
    payload = path.read_bytes()
    _require(payload[:8] == b"\x89PNG\r\n\x1a\n", "Architecture must be a PNG")
    width, height = struct.unpack(">II", payload[16:24])
    _require(width >= 1536, "Architecture native width must be >=1536 pixels")
    _require((width, height) == (metadata["width_px"], metadata["height_px"]),
             "Architecture pixel metadata mismatch")
    _require(metadata["sha256"] == _sha(path), "Architecture hash mismatch")
    _require(metadata.get("review_status") == "passed",
             "Architecture review pending")
    _require(bool(metadata.get("tool") and metadata.get("generated_at_utc")),
             "Architecture actual tool and generation date required")
    return metadata


def check_sources(paper_dir, source):
    """Recompute evidence and verify provenance, citations and artifacts.

    Args:
        paper_dir: Paper output directory.
        source: Read-only archive directory.
    Returns:
        Recomputed summary and automatic check results.
    Raises:
        VerificationError: An invariant fails; input errors propagate.
    """
    from ara_v1_data import load_summary
    from summarize_ara_v1 import check_outputs
    summary = load_summary(source)
    check_outputs(summary, paper_dir)
    sources = _source_provenance(paper_dir, summary)
    content, labels = _tex_document(paper_dir)
    literature = _literature(paper_dir, content)
    count = _claims(paper_dir, (summary, sources, literature, labels))
    check_architecture(paper_dir)
    checks = {"sources": "passed", "generated_artifacts": "passed",
              "labels": "passed", "citations": "passed",
              "claim_locators": count, "architecture": "passed"}
    return summary, checks


def collect_input_hashes(paper_dir, summary):
    """Hash every compilation and verification input, including Git blobs.

    Args:
        paper_dir: Paper directory inside the workspace.
        summary: Recomputed run summary containing source records.
    Returns:
        Sorted repository-relative or git source_id to SHA-256 mapping.
    Raises:
        VerificationError: An input leaves the workspace or has changed.
    """
    fixed = ["paper.tex", "references.bib", "build.ps1", "README.md",
             "requirements-paper.txt", ".gitignore", ".gitattributes",
             "evidence/ara-v1-summary.json", "evidence/figure-manifest.json",
             "evidence/ara-v1-trials.csv", "evidence/claim-traceability.csv",
             "evidence/source-provenance.md",
             "evidence/literature-provenance.md",
             "evidence/architecture-generation.md",
             "figures/ara-architecture.png", "figures/ara-v1-search.pdf",
             "figures/ara-v1-search.png"]
    paths = {_paper_path(paper_dir, name) for name in fixed}
    paths.update(path for path, _ in _tex_inputs(paper_dir))
    paths.update(paper_dir.glob("evidence/*.py"))
    paths.add(SPEC_PATH)
    result = {}
    for path in paths:
        key = _workspace_path(path).relative_to(REPO_ROOT).as_posix()
        result[key] = _sha(path)
    for record in summary["source_records"]:
        key = record["path"]
        if record["source_kind"] == "git_blob":
            key = record["source_id"]
        digest = hashlib.sha256(_read_source(record)).hexdigest()
        _require(digest == record["sha256"], f"Changed source: {key}")
        result[key] = digest
    return dict(sorted(result.items()))


def snapshot_inputs(paper_dir, compare=False):
    """Freeze build inputs, or reject changes from the frozen build manifest.

    Args:
        paper_dir: Paper directory.
        compare: Compare to build-manifest instead of freezing a new snapshot.
    Returns:
        Current input hash mapping.
    Raises:
        VerificationError: Frozen and current input sets or hashes differ.
    """
    from ara_v1_data import load_summary
    current = collect_input_hashes(paper_dir, load_summary(SOURCE_DIR))
    if compare:
        manifest = _read_json(paper_dir / "build/build-manifest.json")
        frozen = manifest["input_hashes"]
        changed = sorted(key for key in set(current) | set(frozen)
                         if current.get(key) != frozen.get(key))
        _require(not changed,
                 f"Build inputs changed; rebuild required: {changed}")
    return current


def _run(command, cwd, environment):
    completed = subprocess.run(command, cwd=cwd, env=environment,
                               capture_output=True, timeout=120)
    _require(completed.returncode == 0,
             f"Command failed ({completed.returncode}): {command[0]}: "
             + completed.stderr.decode("utf-8", errors="replace")[-2000:])
    return completed


def _normalized_text(path):
    content = path.read_text(encoding="utf-8")
    return content.replace("\r\n", "\n").replace("\r", "\n")


def check_pdf(paper_dir, pdf, text_path):
    """Extract the specified PDF anew and bind it to the supplied text.

    Args:
        paper_dir: Paper directory containing build scratch space.
        pdf: Exact PDF to inspect.
        text_path: Existing extraction to compare with fresh output.
    Returns:
        PDF hash, extraction hash, page count and text check status.
    Raises:
        VerificationError: Extraction, content or text identity fails.
    """
    _require(pdf.read_bytes().startswith(b"%PDF-"), f"Invalid PDF: {pdf}")
    tools = discover_tools(os.environ.get("PAPER_TEXBIN"))
    environment = os.environ | prepare_environment(paper_dir, tools)
    build = _paper_path(paper_dir, "build")
    with tempfile.TemporaryDirectory(prefix="verify-", dir=build) as directory:
        extracted = Path(directory) / "current.txt"
        options = []
        if tools.get("pdf_family") == "xpdf":
            options = ["-cfg", str(build / "xpdfrc")]
        _run([tools["pdftotext"], *options, "-enc", "UTF-8", "-layout",
              str(pdf), str(extracted)], paper_dir, environment)
        content = _normalized_text(extracted)
        _require(content == _normalized_text(text_path),
                 "--text does not match text freshly extracted from --pdf")
        digest = _sha(extracted)
    compact = re.sub(r"\s+", "", content)
    for expected in (TITLE, "摘要", "参考文献", "0.089975", "54/100"):
        _require(expected in compact, f"Required PDF text missing: {expected}")
    _require("\ufffd" not in content, "Replacement characters in PDF text")
    pages = content.count("\f")
    _require(pages > 0, "No PDF page boundaries found")
    return {"pdf_sha256": _sha(pdf), "text_sha256": digest,
            "page_count": pages, "pdf_text": "passed"}


def check_review(paper_dir, pdf_result):
    """Require a complete human review bound to the current PDF bytes.

    Args:
        paper_dir: Paper directory.
        pdf_result: Current PDF hash and page count.
    Returns:
        SHA-256 of the accepted review document.
    Raises:
        VerificationError: Review status, hash, timestamp or pages mismatch.
    """
    path = paper_dir / "evidence/review.md"
    review = _metadata(path)
    for key in ("pdf_sha256", "page_count"):
        _require(review.get(key) == pdf_result[key], f"Review {key} mismatch")
    _require(review.get("status") == "passed", "Human review has not passed")
    _require(review.get("render_dpi") == 120, "Review must use 120 dpi pages")
    pages = list(range(1, pdf_result["page_count"] + 1))
    _require(review.get("reviewed_pages") == pages,
             "Review must cover every page once")
    date_string = review["reviewed_at_utc"].replace("Z", "+00:00")
    date = datetime.fromisoformat(date_string)
    _require(date.utcoffset() == timezone.utc.utcoffset(date),
             "Review timestamp must be UTC")
    rendered = sorted(int(path.stem.split("-")[-1])
                      for path in (paper_dir / "build/pages").glob(
                          "page-*.png"))
    _require(rendered == pages, "Rendered page set does not match current PDF")
    return _sha(path)


def _arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-dir", type=Path, default=PAPER_DIR)
    parser.add_argument("--source", type=Path, default=SOURCE_DIR)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--text", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-sources-only", action="store_true")
    modes.add_argument("--require-review", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    """Run verification, writing a report and returning CLI status 0 or 4.

    Args:
        argv: Optional argument list; otherwise use the process arguments.
    Returns:
        Zero for applicable checks passed; four for verification failure.
    Raises:
        SystemExit: argparse rejects malformed or incompatible arguments.
    """
    args = _arguments(argv)
    report = {"status": "failed", "checks": {}, "errors": [],
              "human_review": "not_requested"}
    from summarize_ara_v1 import validate_paths
    try:
        source, paper_dir = validate_paths(args.source, args.paper_dir)
        environment = prepare_environment(paper_dir)
        os.environ.update(environment)
    except (ValueError, OSError) as error:
        report["errors"].append(str(error))
        print(json.dumps(report, ensure_ascii=False))
        return 4
    try:
        _, report["checks"] = check_sources(paper_dir, source)
        if not args.check_sources_only:
            pdf = (args.pdf or paper_dir / "paper.pdf").resolve()
            text_path = (args.text or paper_dir / "build/paper.txt").resolve()
            report.update(check_pdf(paper_dir, pdf, text_path))
        if args.require_review:
            report["review_sha256"] = check_review(paper_dir, report)
            report["human_review"] = "passed"
        report["status"] = "passed"
    except (ValueError, OSError, KeyError, TypeError, IndexError,
            subprocess.SubprocessError) as error:
        report["errors"].append(str(error))
    _write_json(paper_dir / "build/verification.json", report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report["status"] == "passed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
