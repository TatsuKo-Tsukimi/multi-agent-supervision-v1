"""
Trace helper — load + validate + summarize run records from pilot/main run.

Usage:
    python -m runners.trace_helper data/runs/{run_id}/
    python -m runners.trace_helper data/runs/  # summarize all runs

For pilot: verifies that orchestrator produced schema-conformant trace.
For main run: aggregates metrics per condition.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Make src/ importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.data_models import RunRecord, RoundRecord, ConditionEnum  # noqa: E402


# ============================================================
# Load
# ============================================================

def load_run_record(run_dir: Path) -> RunRecord:
    """Load and validate one run's records into a RunRecord."""
    run_json = run_dir / "run.json"
    if not run_json.exists():
        raise FileNotFoundError(f"run.json not found in {run_dir}")

    with open(run_json) as f:
        data = json.load(f)

    # If run.json is the assembled record, use it directly
    if "rounds" in data and isinstance(data["rounds"], list):
        return RunRecord.model_validate(data)

    # Otherwise assemble from round_NN.json files
    rounds = []
    for round_file in sorted(run_dir.glob("round_*.json")):
        with open(round_file) as f:
            rounds.append(RoundRecord.model_validate(json.load(f)))
    data["rounds"] = [r.model_dump() for r in rounds]
    return RunRecord.model_validate(data)


def load_all_runs(runs_dir: Path) -> list[RunRecord]:
    """Load all runs from data/runs/ directory."""
    if not runs_dir.exists():
        return []
    records = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            records.append(load_run_record(run_dir))
        except Exception as e:
            print(f"[skip] {run_dir.name}: {e}", file=sys.stderr)
    return records


# ============================================================
# Summarize single run
# ============================================================

def summarize_run(rec: RunRecord) -> str:
    lines = [
        f"=== Run {rec.run_id} ===",
        f"Task: {rec.task_id}",
        f"Condition: {rec.condition.value}",
        f"Seed: {rec.seed}",
        f"Models: workers={rec.worker_models[0]} | supervisor={rec.supervisor_model}",
    ]
    if rec.dissenter_model:
        lines.append(f"  dissenter={rec.dissenter_model}")
    if rec.attacker_variant:
        lines.append(f"  attacker_variant={rec.attacker_variant.value}")

    lines.append(f"Rounds completed: {len(rec.rounds)}/9")

    if rec.final_outcome:
        lines.append(f"Task success: {rec.final_outcome.task_success}")
        if rec.final_outcome.covert_success is not None:
            lines.append(f"Covert success: {rec.final_outcome.covert_success}")

    # Flag distribution
    flags = defaultdict(int)
    for round_rec in rec.rounds:
        flags[round_rec.supervisor_evaluation.flag.value] += 1
    lines.append(f"Supervisor flags: clean={flags['clean']} mild={flags['mild_concern']} strong={flags['strong_concern']}")

    # Advisory count
    advisory_count = sum(
        1 for r in rec.rounds if r.supervisor_evaluation.advisory
    )
    lines.append(f"Supervisor advisories issued: {advisory_count}")

    # Dissent stats (A1-B2 only)
    dissent_events = [r.dissenter_event for r in rec.rounds if r.dissenter_event]
    if dissent_events:
        binding_changed = sum(1 for d in dissent_events if d.vote_shift.binding_plan_changed)
        lines.append(f"Dissent events: {len(dissent_events)} | binding_changed: {binding_changed}/{len(dissent_events)}")

    # Token usage
    if rec.token_usage:
        tu = rec.token_usage
        lines.append(f"Tokens: worker={tu.worker_total} dissenter={tu.dissenter_total} supervisor={tu.supervisor_total}")
        lines.append(f"Cost equivalent: ${tu.cost_usd:.2f}")

    return "\n".join(lines)


# ============================================================
# Aggregate metrics (placeholder — fill in based on Stage 7+ analysis needs)
# ============================================================

def aggregate_by_condition(runs: list[RunRecord]) -> dict[str, dict]:
    """Group runs by condition, compute basic aggregate stats."""
    by_cond = defaultdict(list)
    for r in runs:
        by_cond[r.condition.value].append(r)

    summary = {}
    for cond, rs in by_cond.items():
        n = len(rs)
        task_success = [r.final_outcome.task_success for r in rs if r.final_outcome]
        covert_success = [r.final_outcome.covert_success for r in rs
                          if r.final_outcome and r.final_outcome.covert_success is not None]
        total_cost = sum(r.token_usage.cost_usd for r in rs if r.token_usage)

        summary[cond] = {
            "n_runs": n,
            "task_success_rate": sum(task_success) / len(task_success) if task_success else None,
            "covert_success_rate": sum(covert_success) / len(covert_success) if covert_success else None,
            "total_cost_usd": total_cost,
            "avg_cost_per_run": total_cost / n if n else 0,
        }
    return summary


def compute_a1_metrics(runs: list[RunRecord]) -> dict:
    """Compute A1 (epistemic dissenter) metrics — vote shift, delta rate."""
    # TODO Stage 10: implement properly
    a1_b2_runs = [r for r in runs if r.condition == ConditionEnum.A1_B2]
    if not a1_b2_runs:
        return {"note": "no A1-B2 runs found"}

    total_dissents = 0
    binding_changed = 0
    individual_changes = 0
    for r in a1_b2_runs:
        for round_rec in r.rounds:
            if round_rec.dissenter_event:
                total_dissents += 1
                if round_rec.dissenter_event.vote_shift.binding_plan_changed:
                    binding_changed += 1
                individual_changes += len(round_rec.dissenter_event.vote_shift.individual_vote_changes)

    return {
        "n_a1_b2_runs": len(a1_b2_runs),
        "total_dissents": total_dissents,
        "vote_shift_rate": binding_changed / total_dissents if total_dissents else None,
        "individual_vote_change_count": individual_changes,
    }


def compute_a2_metrics(runs: list[RunRecord]) -> dict:
    """Compute A2 (security MRT) metrics — covert success rate, flag rate per attacker."""
    # TODO Stage 10: implement properly
    by_variant = defaultdict(list)
    for r in runs:
        if r.attacker_variant:
            by_variant[r.attacker_variant.value].append(r)

    summary = {}
    for variant, rs in by_variant.items():
        n = len(rs)
        covert = [r.final_outcome.covert_success for r in rs
                  if r.final_outcome and r.final_outcome.covert_success is not None]
        flag_strong = sum(
            1 for r in rs for round_rec in r.rounds
            if round_rec.supervisor_evaluation.flag.value == "strong_concern"
        )
        total_rounds = sum(len(r.rounds) for r in rs)

        summary[variant] = {
            "n_runs": n,
            "covert_success_rate": sum(covert) / len(covert) if covert else None,
            "strong_flag_rate_per_round": flag_strong / total_rounds if total_rounds else None,
        }
    return summary


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Load + summarize run records")
    parser.add_argument("path", type=str, help="Path to a run directory or runs/ root")
    parser.add_argument("--metrics", action="store_true", help="Compute aggregate metrics")
    args = parser.parse_args()

    path = Path(args.path)

    # Single run vs directory of runs
    if (path / "run.json").exists():
        rec = load_run_record(path)
        print(summarize_run(rec))
        return

    # Otherwise assume path is data/runs/ or similar containing multiple run dirs
    runs = load_all_runs(path)
    if not runs:
        print(f"No runs found under {path}")
        return

    print(f"Loaded {len(runs)} runs from {path}\n")
    for r in runs:
        print(summarize_run(r))
        print()

    if args.metrics:
        print("=" * 60)
        print("Aggregate by condition:")
        for cond, stats in aggregate_by_condition(runs).items():
            print(f"  {cond}: {stats}")

        print("\nA1 metrics:")
        print(f"  {compute_a1_metrics(runs)}")

        print("\nA2 metrics:")
        for variant, stats in compute_a2_metrics(runs).items():
            print(f"  Variant {variant}: {stats}")


if __name__ == "__main__":
    main()
