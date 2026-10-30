"""Standalone, escaped report with original files and per-arm evidence."""

import csv
import json
from pathlib import Path
import subprocess


def render(payload, root: Path):
    with (root / "metrics.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sample", "kind", "arm", "complete", "facts_found", "fact_count", "source_correct", "ordered", "local_seconds", "ocr_pages", "ocr_upper_yuan", "recorded_ocr_seconds"])
        for row in payload["results"]:
            for arm, result in row["arms"].items():
                metrics = result["metrics"]
                writer.writerow([row["sample"]["id"], row["sample"]["kind"], arm, result["complete"], metrics["facts_found"],
                                 metrics["fact_count"], metrics["source_correct"], metrics["ordered"], result["local_seconds"],
                                 result["ocr_pages"], result["ocr_upper_yuan"], result["recorded_ocr_seconds"]])
    previews = root / "previews"
    previews.mkdir(exist_ok=True)
    for row in payload["results"]:
        source = root / "samples" / row["sample"]["path"]
        if source.suffix == ".pdf":
            output = previews / row["sample"]["id"]
            if not output.with_suffix(".png").exists():
                try:
                    subprocess.run(["pdftoppm", "-f", "1", "-singlefile", "-scale-to", "1000", "-png", str(source), str(output)],
                                   check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (OSError, subprocess.SubprocessError):
                    continue
            row["sample"]["preview"] = f"previews/{output.name}.png"
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = Path(__file__).with_name("report.html").read_text(encoding="utf-8")
    (root / "report.html").write_text(template.replace("__EXPERIMENT_DATA__", data), encoding="utf-8")
