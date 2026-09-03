"""
Wires the nodes in src/nodes.py into a LangGraph StateGraph.

Flow:
    check_reputation --(reputation_gate)--> [known_good | known_bad] -> decide_route
                                          -> [needs_conversation] -> greet -> classify_turn
    classify_turn --(classify_gate)--> [decide] -> decide_route
                                     -> [listen_again] -> classify_turn (loop)
"""
from __future__ import annotations
from langgraph.graph import StateGraph, END

from .state import CallState
from .nodes import (
    check_reputation,
    reputation_gate,
    greet,
    classify_turn,
    classify_gate,
    decide_route,
    simulated_listen,
)


def build_graph():
    graph = StateGraph(CallState)

    graph.add_node("check_reputation", check_reputation)
    graph.add_node("greet", greet)
    graph.add_node("classify_turn", classify_turn)
    graph.add_node("decide_route", decide_route)
    graph.add_node("simulated_listen", simulated_listen)

    graph.set_entry_point("check_reputation")

    graph.add_conditional_edges(
        "check_reputation",
        reputation_gate,
        {
            "known_good": "decide_route",
            "known_bad": "decide_route",
            "needs_conversation": "greet",
        },
    )

    graph.add_edge("greet", "classify_turn")

    graph.add_conditional_edges(
        "classify_turn",
        classify_gate,
        {
            "decide": "decide_route",
            "listen_again": "simulated_listen",
        },
    )
    graph.add_edge("simulated_listen", "classify_turn")

    graph.add_edge("decide_route", END)

    return graph.compile()
