"""Independent panel judge for shadow-lane briefing pairs, using Claude.

Reads shadow report JSON files (the `report` column of evaluation_runs, one
JSON object per file), presents each champion/candidate briefing pair to
Claude Fable blind and in randomized order, and reports which draft the
independent judge prefers. This exists because the production critic is a
GPT model judging GPT-family output — a different-family judge exposes
self-preference bias in the daily verdicts.

Usage:
  export ANTHROPIC_API_KEY=...   # or `ant auth login`
  python judge_panel.py shadow_0825.json shadow_0823.json --out panel_verdicts.json
"""

import argparse
import hashlib
import json
import re
import sys

import anthropic

JUDGE_MODEL = "claude-fable-5"

RUBRIC = """You are judging two drafts of a cloud-security briefing for senior
cloud engineers (casual, cynical, practitioner voice; Telegram-style markdown).

Judge on, in order of weight:
1. Grounding: claims scoped to real prerequisites, no invented specifics,
   PoC vs confirmed exploitation labeled honestly.
2. Actionability: concrete versions, patches, detection guidance.
3. Editorial quality: opener with a thesis, memorable closing takeaway,
   punchy-but-not-performative voice, varied structure.
4. Link discipline: every story linked exactly once, no dangling references.

Return STRICT JSON only:
{"winner": "A" | "B" | "tie", "confidence": "low" | "medium" | "high",
 "margin": 0-10, "reasons": ["short concrete reason", ...]}"""


def judge_pair(client: anthropic.Anthropic, pair_id: str, champion: str, candidate: str) -> dict:
    # Deterministic blind ordering: half the pairs show candidate first.
    flipped = int(hashlib.sha256(pair_id.encode()).hexdigest(), 16) % 2 == 1
    draft_a, draft_b = (candidate, champion) if flipped else (champion, candidate)

    response = client.beta.messages.create(
        model=JUDGE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=RUBRIC,
        messages=[
            {
                "role": "user",
                "content": f"DRAFT A:\n{draft_a}\n\n----\n\nDRAFT B:\n{draft_b}",
            }
        ],
    )
    if response.stop_reason == "refusal":
        return {"pair_id": pair_id, "error": "judge_refusal"}

    text = next((b.text for b in response.content if b.type == "text"), "")
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {"pair_id": pair_id, "error": "unparseable", "raw": text[:300]}
    verdict = json.loads(match.group(0))

    winner_slot = str(verdict.get("winner", "tie"))
    if winner_slot == "A":
        winner = "candidate" if flipped else "champion"
    elif winner_slot == "B":
        winner = "champion" if flipped else "candidate"
    else:
        winner = "tie"
    return {
        "pair_id": pair_id,
        "winner": winner,
        "confidence": verdict.get("confidence"),
        "margin": verdict.get("margin"),
        "reasons": verdict.get("reasons", []),
        "judge_model": response.model,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", help="shadow report JSON files")
    parser.add_argument("--out", default="panel_verdicts.json")
    args = parser.parse_args()

    client = anthropic.Anthropic()
    verdicts = []
    for path in args.reports:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        champion = str((report.get("champion") or {}).get("markdown") or "").strip()
        candidate = str((report.get("candidate") or {}).get("markdown") or "").strip()
        if not champion or not candidate:
            print(f"skip {path}: missing draft text", file=sys.stderr)
            continue
        verdict = judge_pair(client, path, champion, candidate)
        verdicts.append({"report": path, **verdict})
        print(json.dumps(verdicts[-1], ensure_ascii=False))

    wins = {"champion": 0, "candidate": 0, "tie": 0}
    for v in verdicts:
        if v.get("winner") in wins:
            wins[v["winner"]] += 1
    summary = {"judge": JUDGE_MODEL, "pairs": len(verdicts), **wins}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"summary": summary, "verdicts": verdicts}, handle, indent=2)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
