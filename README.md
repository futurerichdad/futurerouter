# FutureRouter

A custom LangGraph agent that screens inbound phone calls, classifies the
caller as a **real person**, a **solicitor/telemarketer**, or an
**automated robocall**, and routes the call accordingly:

- Real caller → ring through to the user
- Robocall / automated message → send to voicemail
- Solicitor → block

This repo currently implements and evaluates the **agent's decision-making
graph** against text call transcripts. Live telephony (Twilio/Telnyx +
Deepgram ASR + ElevenLabs TTS) is a planned follow-on phase once the
agent's classification logic is solid.

## Why a graph instead of one prompt

A phone call is inherently turn-based and can branch (the caller goes
silent, gives an ambiguous answer, or reputation data alone is already
conclusive). Modeling this as an explicit LangGraph `StateGraph` -- rather
than one large prompt -- makes each decision point small, independently
testable, and traceable:

```
check_reputation --(reputation_gate)--> known_good/known_bad -> decide_route
                                      -> needs_conversation -> greet -> classify_turn
classify_turn --(classify_gate)--> confident -> decide_route
                                 -> uncertain -> simulated_listen -> classify_turn (loop)
```

Reputation lookups run first and are essentially free -- they short-circuit
the loop for numbers already known to be good or bad, so the LLM
conversation (the expensive, higher-latency step) only runs when it's
actually needed.

## Design choice: biased toward false-pass over false-block

`decide_route` in `src/nodes.py` is deliberately conservative: any
low-confidence or ambiguous classification defaults to **ringing through
to the user** rather than being silently blocked. Blocking a legitimate
call is a worse failure than letting one extra spam call through, so the
routing logic is asymmetric on purpose.

## Project structure

```
src/
  state.py    - shared CallState schema threaded through every node
  tools.py    - reputation lookup (stubbed in-memory; swap for Twilio Lookup later)
  nodes.py    - each graph node: check_reputation, greet, classify_turn, decide_route, ...
  graph.py    - wires nodes into the LangGraph StateGraph
data/
  sample_transcripts.json  - example calls for run_demo.py
eval/
  golden_set.json     - hand-labeled expected outcomes
  eval_harness.py     - runs the agent against the golden set, reports
                        accuracy, false-block rate, and false-pass rate separately
run_demo.py           - runs the agent against sample_transcripts.json and prints
                        the full transcript + routing decision for each call
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then add your ANTHROPIC_API_KEY
```

## Run the demo

```bash
python run_demo.py
```

## Run the evaluation harness

```bash
python eval/eval_harness.py
```

Reports per-case pass/fail plus overall accuracy, false-block rate, and
false-pass rate -- the two error types that matter most differently for
this product.

## Roadmap

- [x] LangGraph agent with conditional routing and reputation short-circuit
- [x] Offline eval harness with false-block/false-pass tracking
- [ ] LangSmith tracing integration
- [ ] Live telephony: Twilio Voice + Media Streams
- [ ] Streaming ASR (Deepgram) + TTS (ElevenLabs) for real-time conversation
- [ ] Shared blocklist/allowlist persistence (Postgres/Redis)
- [ ] User dashboard for reviewing call decisions
