"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, aggregate, load_spec, per_item
from lab9.graders import GRADERS


def _bad_number(v: Any) -> bool:
    return isinstance(v, float) and not math.isfinite(v)


def _verdict(v: Any) -> bool:
    """A grader verdict: True/False (or 1/0). None, NaN, a missing key or any other value is not one."""
    return isinstance(v, bool) or (isinstance(v, (int, float)) and v in (0, 1))


def _key(r: dict) -> tuple:
    return (str(r.get("item_id")), r.get("seed"))


def _report_problems(rep: dict, spec: dict, who: str) -> list[str]:
    """Checks one report against itself: NaN, row count vs manifest, duplicates, crashed graders,
    and an aggregate that its own rows do not support (a hand-edited metrics block)."""
    out: list[str] = []
    metrics, rows, man = rep.get("metrics") or {}, rep.get("rows") or [], rep.get("manifest") or {}
    out += [f"{who} metric {k} is {v} (NaN/inf)" for k, v in metrics.items() if _bad_number(v)]
    seeds = man.get("seeds") or []
    want = (man.get("items") or 0) * len(seeds)
    if len(rows) != want:
        out.append(f"{who} has {len(rows)} rows, manifest says {man.get('items')} items x {len(seeds)} seeds = {want}")
    keys = [_key(r) for r in rows]
    dup = sorted({k for k in keys if keys.count(k) > 1}, key=str)
    if dup:
        out.append(f"{who} duplicate (item, seed) rows: {dup[:5]}")
    stray = sorted({k[1] for k in keys} - set(seeds), key=str)
    if stray:
        out.append(f"{who} rows with seeds not in the manifest: {stray}")
    if rep.get("evaluator_failures"):
        out.append(f"{who} has {len(rep['evaluator_failures'])} evaluator failure(s): {rep['evaluator_failures'][0]}")
    graded = [m["id"] for m in spec.get("metrics", []) if m["id"] in GRADERS]
    for m in graded:
        bad = [r for r in rows if not _verdict(r.get(m))]
        if bad:
            out.append(f"{who} {len(bad)} row(s) with no boolean {m} (crashed or edited grader), "
                       f"first {_key(bad[0])}={bad[0].get(m)!r}")
    if rows and not out:
        # The thresholds read the metrics block; the bootstrap and slices read the rows.
        # Both must tell the same story, or someone edited one of them.
        try:
            again = aggregate([{**r, "cost": r.get("cost", 0.0)} for r in rows],
                              [m for m in graded if m in rows[0]], k_values=spec.get("pass_k", []))
        except Exception as e:  # noqa: BLE001
            return out + [f"{who} rows cannot be re-aggregated: {e}"]
        has_cost = all("cost" in r for r in rows)
        for k, v in metrics.items():
            if k not in again or (k == "cost_per_success" and not has_cost):
                continue
            w = again[k]
            if (v is None) != (w is None) or (v is not None and abs(float(v) - float(w)) > 1e-6):
                out.append(f"{who} metric {k}={v} but its rows give {w} (metrics edited?)")
    return out


def _looser(new: dict | None, old: dict, what: str) -> list[str]:
    """Ratchet for one metric or slice entry: it may only get stricter."""
    if new is None:
        return [f"{what} removed"]
    out = []
    nt = (new.get("threshold") if "threshold" in new else new) or {}
    ot = (old.get("threshold") if "threshold" in old else old) or {}
    if "max" in ot and ("max" not in nt or nt["max"] > ot["max"]):
        out.append(f"{what} max {ot['max']} -> {nt.get('max')}")
    if "min" in ot and ("min" not in nt or nt["min"] < ot["min"]):
        out.append(f"{what} min {ot['min']} -> {nt.get('min')}")
    if "max_increase" in ot and ("max_increase" not in nt or nt["max_increase"] > ot["max_increase"]):
        out.append(f"{what} max_increase {ot['max_increase']} -> {nt.get('max_increase')}")
    for flag in ("no_significant_drop", "no_new_failures"):
        if ot.get(flag) and not nt.get(flag):
            out.append(f"{what} {flag} dropped")
    if old.get("blocking") and not new.get("blocking"):
        out.append(f"{what} no longer blocking")
    return out


def _ratchet(spec: dict, base_spec: dict) -> list[str]:
    out = []
    if spec.get("eval_set_version") != base_spec.get("eval_set_version"):
        # check() already proved the eval set itself is unchanged (same sha), so a new label
        # is only a way around the ratchet.
        out.append(f"ratchet: eval_set_version {base_spec.get('eval_set_version')} -> "
                   f"{spec.get('eval_set_version')} without a new eval set")
    new_m = {m["id"]: m for m in spec.get("metrics", [])}
    for m in base_spec.get("metrics", []):
        out += [f"ratchet: metric {p}" for p in _looser(new_m.get(m["id"]), m, m["id"])]
    new_s = {(x["name"], x.get("metric")): x for x in spec.get("slices") or []}
    for x in base_spec.get("slices") or []:
        out += [f"ratchet: slice {p}" for p in _looser(new_s.get((x["name"], x.get("metric"))), x,
                                                       f"{x['name']}/{x.get('metric')}")]
    ob, nb = base_spec.get("bootstrap") or {}, spec.get("bootstrap") or {}
    if nb.get("confidence", 0.95) > ob.get("confidence", 0.95):
        out.append(f"ratchet: bootstrap confidence {ob.get('confidence')} -> {nb.get('confidence')} (wider CI)")
    if not set(base_spec.get("seeds") or []) <= set(spec.get("seeds") or []):
        out.append(f"ratchet: seeds {base_spec.get('seeds')} -> {spec.get('seeds')}")
    if not set(base_spec.get("pass_k") or []) <= set(spec.get("pass_k") or []):
        out.append(f"ratchet: pass_k {base_spec.get('pass_k')} -> {spec.get('pass_k')}")
    return out


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems = _report_problems(pr, spec, "PR")
    problems += _report_problems(base, spec, "baseline")
    pm, bm = pr.get("manifest") or {}, base.get("manifest") or {}
    if pm.get("items") != bm.get("items") or sorted(pm.get("seeds") or []) != sorted(bm.get("seeds") or []):
        problems.append(f"manifest differs from baseline: items {pm.get('items')} vs {bm.get('items')}, "
                        f"seeds {pm.get('seeds')} vs {bm.get('seeds')}")
    pk, bk = {_key(r) for r in pr.get("rows") or []}, {_key(r) for r in base.get("rows") or []}
    if pk != bk:
        problems.append(f"(item, seed) set differs from baseline: missing {sorted(bk - pk, key=str)[:5]}, "
                        f"extra {sorted(pk - bk, key=str)[:5]}")
    bslice = {r.get("item_id"): sorted(r.get("slices") or []) for r in base.get("rows") or []}
    moved = sorted({str(r.get("item_id")) for r in pr.get("rows") or []
                    if r.get("item_id") in bslice and sorted(r.get("slices") or []) != bslice[r.get("item_id")]})
    if moved:
        problems.append(f"slice tags differ from baseline for {moved[:5]}")
    if base_spec is not None:
        problems += _ratchet(spec, base_spec)
    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for sl in spec.get("slices") or []:
        name, metric = sl["name"], sl["metric"]
        sid = f"slice:{name}/{metric}"
        a, b = per_item(pr, metric, slice_name=name), per_item(base, metric, slice_name=name)
        verdict, note, val = "ok", "", None
        if not a:
            verdict = "missing"                         # a slice with no rows proves nothing
        else:
            val = round(sum(a.values()) / len(a), 4)
            if ("min" in sl and val < sl["min"]) or ("max" in sl and val > sl["max"]):
                verdict = "fail"
            keys = sorted(set(a) & set(b))
            if sl.get("no_significant_drop") and keys:
                d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                                   n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027),
                                                   confidence=bs.get("confidence", 0.95))
                note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                if hi < 0:
                    verdict = "fail"
        if verdict != "ok" and sl.get("blocking"):
            blocked.append(sid)
        th = {k: sl[k] for k in ("min", "max", "no_significant_drop") if k in sl}
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if sl.get("blocking") else "warn")
        lines.append(f"  {sid:<34} {val!s:<8} n={len(a):<3} {json.dumps(th):<36} {note:<28} {label}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if th.get("no_new_failures") and val is not None:
            # Deterministic harness (A/A delta 0): an item green on every seed in the baseline that
            # now fails any seed is a regression, even when fixes elsewhere keep the average flat.
            a, b = per_item(pr, mid), per_item(base, mid)
            newly = sorted(k for k in a if b.get(k) == 1.0 and a[k] < 1.0)
            if newly:
                verdict = "fail"
                note = (note + " " if note else "") + f"new fails {','.join(newly[:3])}{'…' if len(newly) > 3 else ''}"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
