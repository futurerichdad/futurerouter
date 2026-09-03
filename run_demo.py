"""
Runs the FutureRouter LangGraph agent against sample text transcripts
(data/sample_transcripts.json) instead of a live phone call -- this is the
fast way to develop and demo the agent's classification/routing logic
before wiring up real telephony (Twilio/Deepgram/ElevenLabs).

Usage:
    python run_demo.py
"""
import json
from dotenv import load_dotenv

from src.graph import build_graph

load_dotenv()


def main():
    graph = build_graph()

    with open("data/sample_transcripts.json") as f:
        samples = json.load(f)

    for sample in samples:
        initial_state = {
            "caller_number": sample["caller_number"],
            "transcript": [],
            "turn_count": 0,
            "max_turns": 3,
            "scripted_caller_lines": sample["scripted_caller_lines"],
        }

        final_state = graph.invoke(initial_state)

        print(f"\n=== {sample['id']} ({sample['caller_number']}) ===")
        for turn in final_state.get("transcript", []):
            print(f"  {turn['speaker']}: {turn['text']}")
        print(f"  -> category: {final_state.get('category')} "
              f"(confidence {final_state.get('confidence', 0):.2f})")
        print(f"  -> reasoning: {final_state.get('reasoning', 'n/a (reputation-based decision)')}")
        print(f"  -> ROUTE: {final_state.get('route')}")


if __name__ == "__main__":
    main()
