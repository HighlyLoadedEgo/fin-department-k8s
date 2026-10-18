"""Evaluate the actual dashboard PromQL against hand-checked business examples.

Run: python3 tests/monitoring/check_dashboards.py --docker
Requires PyYAML and either promtool on PATH or Docker.
"""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ROOT / "infrastructure/base/controllers/monitoring/dashboards"


def walk_panels(panels):
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels", []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true")
    args = parser.parse_args()
    dashboards = {}
    for path in DASHBOARDS.glob("*.yaml"):
        cm = yaml.safe_load(path.read_text())
        for value in cm.get("data", {}).values():
            dashboard = json.loads(value)
            dashboards[dashboard["uid"]] = dashboard

    fixtures = json.loads(Path(__file__).with_name("business_cases.json").read_text())
    tests = []
    for case in fixtures:
        dashboard = dashboards[case["dashboard"]]
        panel = next(p for p in walk_panels(dashboard["panels"]) if p["id"] == case["panel"])
        target = next(t for t in panel["targets"] if t["refId"] == case.get("ref", "A"))
        expr = target["expr"].replace("$__rate_interval", "5m").replace("$__range", "10m")
        tests.append({
            "name": case["name"],
            "interval": "1m",
            "input_series": case["series"],
            "promql_expr_test": [{
                "expr": expr,
                "eval_time": "10m",
                "exp_samples": case["expected"],
            }],
        })

    # Parse every expression too: empty data is valid, syntax errors are not.
    for dashboard in dashboards.values():
        for panel in walk_panels(dashboard["panels"]):
            for target in panel.get("targets", []):
                if "expr" not in target:
                    continue
                expr = target["expr"].replace("$__rate_interval", "5m").replace("$__range", "10m")
                tests.append({
                    "name": f"syntax: {dashboard['uid']} / {panel['id']} / {target['refId']}",
                    "promql_expr_test": [{
                        "expr": f"({expr}) and on() (vector(0) > 1)",
                        "eval_time": "10m", "exp_samples": [],
                    }],
                })
    with tempfile.TemporaryDirectory(prefix="fin-dashboard-tests-") as directory:
        path = Path(directory) / "cases.yaml"
        path.write_text(yaml.safe_dump({"rule_files": [], "evaluation_interval": "1m", "tests": tests}))
        command = ["promtool", "test", "rules", str(path)]
        if args.docker:
            command = [
                "docker", "run", "--rm", "--network=none", "--entrypoint=/bin/promtool",
                "-v", f"{directory}:/tests:ro", "prom/prometheus:v3.5.0",
                "test", "rules", "/tests/cases.yaml",
            ]
        subprocess.run(command, check=True)
    print(f"Verified {len(fixtures)} business cases against dashboard queries.")


if __name__ == "__main__":
    main()
