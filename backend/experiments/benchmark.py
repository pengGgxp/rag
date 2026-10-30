"""Isolated comparison CLI. Never imports application DB, tasks or accounting."""

import argparse
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import pdfplumber
from app.docmind import AliyunDocMind
from app.parsed_document import ParsedDocument
from .checkpoints import Checkpoints, BudgetError, UncertainCall
from .fixtures import generate
from .selective_parser import Policy, page_decision

ROOT = Path(__file__).resolve().parents[1]


def compact(text):
    return re.sub(r"[\W_]", "", text, flags=re.UNICODE).casefold()


def calibrate(root, manifest):
    outcomes = []
    for minimum in (16, 24, 40):
        policy = Policy(min_chars=minimum)
        checks = []
        for sample in manifest["calibration"]:
            with pdfplumber.open(root / sample["path"]) as pdf:
                page = pdf.pages[0]
                decision = page_decision(page, page.extract_text() or "", policy)
                checks.append({"sample": sample["path"], "expected": sample["expected"], **decision})
        outcomes.append({"policy": asdict(policy), "correct": sum(c["route"] == c["expected"] for c in checks), "checks": checks})
    # Highest held-out routing accuracy, ties prefer the original 24-character guard.
    best = max(outcomes, key=lambda x: (x["correct"], x["policy"]["min_chars"] == 24))
    if best["correct"] != len(manifest["calibration"]):
        raise ValueError("Calibration failed; inspect samples before running scored evaluation")
    return {"chosen": best["policy"], "trials": outcomes}


def conversion(source, root, arm, policy):
    output = root / f"current-{arm}.json"
    started = time.monotonic()
    subprocess.run([sys.executable, "-m", "experiments.worker", str(source), str(output), "--arm", arm,
                    "--policy", str(policy)], cwd=ROOT, check=True, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = json.loads(output.read_text(encoding="utf-8"))
    result["local_seconds"] = round(time.monotonic() - started, 4)
    return result


def verified_rate(rate, field):
    if not rate or not rate.get("source") or not rate.get("verified_at"):
        raise BudgetError(f"Missing verified tariff: {field}")
    value = Decimal(str(rate[field]))
    if not value.is_finite() or value < 0:
        raise BudgetError("Invalid tariff")
    return value


def fulfill(request, root, checkpoints, rate):
    price = verified_rate(rate, "upper_yuan_per_page")
    digest = request["hash"]
    key = "docmind-v1:" + digest
    row = checkpoints.get(key)
    if row and row["state"] == "complete":
        parsed = json.loads(row["result"])
    else:
        if row and not row["task_id"]:
            raise UncertainCall("Prior submission is uncertain; refusing to resubmit")
        if not row:
            if not checkpoints.reserve(key, "ocr", price * request["pages"]):
                raise UncertainCall("Concurrent paid submission")
            # Reserve is committed before the only non-idempotent operation.
            provider = AliyunDocMind()
            try:
                task = provider.submit((root / "ocr-inputs" / f"{digest}.pdf").read_bytes(), request["pages"])
            except Exception:
                checkpoints.update(key, state="uncertain")
                raise
            checkpoints.update(key, task_id=task, state="submitted")
        else:
            provider, task = AliyunDocMind(), row["task_id"]
        started = time.monotonic()
        parsed = asdict(provider.collect(task, request["pages"], lambda: None))
        checkpoints.finish(key, parsed, time.monotonic() - started)
    folder = root / "ocr-results"
    folder.mkdir(exist_ok=True)
    (folder / f"{digest}.json").write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")


def metrics(sample, result):
    parsed = ParsedDocument(**result["parsed"])
    text = compact(parsed.text)
    hits = [compact(fact) in text for fact in sample.get("facts", [])]
    positions = [text.find(compact(fact)) for fact in sample.get("order", [])]
    ordered = bool(positions) and all(p >= 0 for p in positions) and positions == sorted(positions)
    source_hits = []
    locations = parsed.source_locations or parsed.pages
    checks = []
    for index, fact in enumerate(sample.get("facts", [])):
        expected = sample.get("expected_locations", [])[index]
        # Check the fact inside its own source span, not a chunk overlapping another source.
        evidence = [location for location in locations if
                    compact(fact) in compact(parsed.text[location["char_start"]:location["char_end"]])]
        source_hit = bool(expected) and any(
            all(location.get(key) == value for key, value in expected.items()) for location in evidence)
        source_hits.append(source_hit)
        checks.append({"fact": fact, "found": hits[index], "expected_location": expected,
                       "source_correct": source_hit, "actual_locations": evidence})
    return {"fact_hits": hits, "facts_found": sum(hits), "fact_count": len(hits), "ordered": ordered,
            "source_hits": source_hits, "source_correct": sum(source_hits),
            "checks": checks,
            "compatibility_radicals": len(re.findall(r"[\u2f00-\u2fd5]", parsed.text)),
            "duplicate_fact_occurrences": [text.count(compact(fact)) for fact in sample.get("facts", [])]}


def run(root, live=False, rate_file=None, additional_manifest=None):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    samples_dir = root / "samples"
    if not (samples_dir / "manifest.json").exists():
        generate(samples_dir)
    manifest = json.loads((samples_dir / "manifest.json").read_text(encoding="utf-8"))
    if additional_manifest:
        manifest["samples"].extend(json.loads(Path(additional_manifest).read_text(encoding="utf-8")))
    if len({s["id"] for s in manifest["samples"]}) != len(manifest["samples"]):
        raise ValueError("Duplicate sample IDs")
    calibration = calibrate(samples_dir, manifest)
    policy_file = root / "policy.json"
    if policy_file.exists() and json.loads(policy_file.read_text(encoding="utf-8")) != calibration["chosen"]:
        raise ValueError("Frozen calibration changed; use a new output directory")
    policy_file.write_text(json.dumps(calibration["chosen"], indent=2), encoding="utf-8")
    checkpoints = Checkpoints(root / "costs.sqlite")
    rates = json.loads(Path(rate_file).read_text(encoding="utf-8")) if rate_file else {}
    results = []
    for sample in manifest["samples"]:
        source = (samples_dir / sample["path"]).resolve()
        if not source.is_relative_to(samples_dir.resolve()) or source.suffix.lower() not in (".docx", ".pdf"):
            raise ValueError("Sample must be a local document inside the experiment")
        if sample.get("source_sha256") and hashlib.sha256(source.read_bytes()).hexdigest() != sample["source_sha256"]:
            raise ValueError("Original sample checksum changed")
        sample.pop("questions", None)
        row = {"sample": sample, "arms": {}}
        for arm in ("baseline", "candidate"):
            first = conversion(source, root, arm, policy_file)
            errors = []
            if live:
                for request in first["ocr_requests"]:
                    try:
                        fulfill(request, root, checkpoints, rates.get("ocr"))
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                result = conversion(source, root, arm, policy_file) if first["ocr_requests"] else first
            else:
                result = first
            result["external_errors"] = errors
            result["first_local_seconds"] = first["local_seconds"]
            calls = {r["hash"]: r for r in result["ocr_calls"]}
            result["ocr_pages"] = sum(r["pages"] for r in calls.values())
            result["ocr_upper_yuan"] = (float(verified_rate(rates["ocr"], "upper_yuan_per_page") * result["ocr_pages"])
                                         if rates.get("ocr") else None)
            result["recorded_ocr_seconds"] = round(sum((checkpoints.get("docmind-v1:" + digest) or {}).get("seconds", 0)
                                                       for digest in calls), 4)
            result["metrics"] = metrics(sample, result)
            row["arms"][arm] = result
        results.append(row)
        (root / "progress.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{sample['id']}: baseline={row['arms']['baseline']['complete']} candidate={row['arms']['candidate']['complete']}", flush=True)
    payload = {"version": 2, "scope": "extraction_and_ocr_only", "calibration": calibration, "results": results, "budget": checkpoints.summary(),
               "rates": rates, "paid_calls_enabled": live,
               "live_ocr": any(c["state"] == "complete" for c in checkpoints.summary()["calls"]),
               "production_changed": False}
    (root / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    from .report import render
    render(payload, root)
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live-ocr", action="store_true")
    parser.add_argument("--rates", type=Path)
    parser.add_argument("--additional-manifest", type=Path)
    args = parser.parse_args()
    run(args.output, args.live_ocr, args.rates, args.additional_manifest)


if __name__ == "__main__":
    main()
