"""
Offline evaluation harness for the FutureRouter agent.

Runs the graph against eval/golden_set.json (hand-labeled expected
category/route per call) and reports accuracy plus, critically, the
false-block rate and false-pass rate separately -- the two failure
modes matter very differently for this product (blocking a real
caller is worse than letting one spam call through).

Usage:
    python eval/eval_harness.py
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from src.graph import build_graph

load_dotenv()


def run_eval():
    graph = build_graph()

    with open(os.path.join(os.path.dirname(__file__), "golden_set.json")) as f:
        golden_set = json.load(f)

    results = []
    for case in golden_set:
        initial_state = {
            "caller_number": case["caller_number"],
            "transcript": [],
            "turn_count": 0,
            "max_turns": 3,
            "scripted_caller_lines": case["scripted_caller_lines"],
        }
        final_state = graph.invoke(initial_state)

        actual_category = final_state.get("category")
        actual_route = final_state.get("route")
        correct = actual_route == case["expected_route"]

        # False block: we blocked a call that should have passed through or gone to voicemail.
        false_block = actual_route == "block" and case["expected_route"] != "block"
        # False pass: we passed through (or voicemailed) a call that should have been blocked.
        false_pass = actual_route != "block" and case["expected_route"] == "block"

        results.append({
            "id": case["id"],
            "expected_route": case["expected_route"],
            "actual_route": actual_route,
            "expected_category": case["expected_category"],
            "actual_category": actual_category,
            "correct": correct,
            "false_block": false_block,
            "false_pass": false_pass,
        })

    total = len(results)
    accuracy = sum(r["correct"] for r in results) / total
    false_block_rate = sum(r["false_block"] for r in results) / total
    false_pass_rate = sum(r["false_pass"] for r in results) / total

    print(f"{'ID':<15} {'EXPECTED':<15} {'ACTUAL':<15} {'OK?'}")
    for r in results:
        mark = "PASS" if r["correct"] else "FAIL"
        print(f"{r['id']:<15} {r['expected_route']:<15} {str(r['actual_route']):<15} {mark}")

    print(f"\nAccuracy: {accuracy:.0%} ({sum(r['correct'] for r in results)}/{total})")
    print(f"False-block rate: {false_block_rate:.0%}  (blocked a call that shouldn't have been)")
    print(f"False-pass rate:  {false_pass_rate:.0%}  (let through a call that should've been blocked)")

    return results


if __name__ == "__main__":
    run_eval()
