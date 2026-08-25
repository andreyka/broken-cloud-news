"""Evaluate an analyst model against held-out analyst_eval.jsonl labels.

Hits any OpenAI-compatible endpoint (vLLM serving the merged model, the NVFP4
requant, or the stock base for a zero-shot baseline) and reports: JSON validity,
relevance-score MAE, within-1 accuracy, and tag Jaccard overlap vs the stored
GPT labels. Stdlib only — no pip installs needed.

  python eval_analyst.py --data analyst_eval.jsonl \
    --base-url http://localhost:8000/v1 --model <served-name> --concurrency 4
"""

import argparse
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def chat(base_url: str, model: str, messages: list[dict], timeout: float) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 400,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    return body["choices"][0]["message"]["content"] or ""


def parse_json_block(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                out = json.loads(match.group(0))
                return out if isinstance(out, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=0, help="0 = all rows")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", default="eval_results.jsonl")
    args = parser.parse_args()

    rows = []
    with open(args.data, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if args.limit:
        rows = rows[: args.limit]

    def run_one(row: dict) -> dict:
        messages = [m for m in row["messages"] if m["role"] != "assistant"]
        label = json.loads(row["messages"][-1]["content"])
        try:
            raw = chat(args.base_url, args.model, messages, args.timeout)
            error = None
        except Exception as exc:  # noqa: BLE001 - record and continue
            raw, error = "", f"{type(exc).__name__}: {exc}"
        pred = parse_json_block(raw)
        return {
            "metadata": row.get("metadata", {}),
            "label": label,
            "prediction": pred,
            "raw": raw if pred is None else None,
            "error": error,
        }

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(run_one, rows))

    total = len(results)
    errors = sum(1 for r in results if r["error"])
    valid = [r for r in results if r["prediction"] is not None]
    scored = [
        r
        for r in valid
        if isinstance(r["prediction"].get("relevance_score"), (int, float))
    ]
    abs_errs = [
        abs(float(r["prediction"]["relevance_score"]) - float(r["label"]["relevance_score"]))
        for r in scored
    ]
    within_1 = sum(1 for e in abs_errs if e <= 1)
    jaccards = []
    for r in valid:
        pred_tags = {str(t).lower() for t in r["prediction"].get("tags") or []}
        label_tags = {str(t).lower() for t in r["label"].get("tags") or []}
        if pred_tags or label_tags:
            jaccards.append(len(pred_tags & label_tags) / len(pred_tags | label_tags))

    summary = {
        "model": args.model,
        "total": total,
        "request_errors": errors,
        "json_valid_rate": round(len(valid) / total, 4) if total else 0,
        "score_parse_rate": round(len(scored) / total, 4) if total else 0,
        "score_mae": round(sum(abs_errs) / len(abs_errs), 3) if abs_errs else None,
        "score_within_1_rate": round(within_1 / len(abs_errs), 4) if abs_errs else None,
        "tag_jaccard_mean": round(sum(jaccards) / len(jaccards), 4) if jaccards else None,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        for r in results:
            handle.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
