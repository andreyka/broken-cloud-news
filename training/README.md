# Local-model migration + QLoRA runbook

Goal: move LLM roles off OpenAI onto Qwen served by vLLM on the DGX Spark
(192.168.0.9), using the existing `model_bridge` (nginx on the LattePanda,
currently proxying to `192.168.0.9:8001`) and the existing shadow lane for
promotion decisions.

## 0. Fix shadow scoring first

The Apr 25 – May 2 Qwen3.6-35B shadow runs all recorded
`champion_score=0, candidate_score=0, both release gates failed` — the lane
was not producing a usable signal, so the May 3 rollback to OpenAI proved
nothing about local model quality. Re-run one shadow cycle against the
current champion and confirm nonzero champion scores before trusting any
candidate verdict.

## 1. Serve on the Spark (zero-shot baseline)

```bash
vllm serve unsloth/Qwen3.8-27B-NVFP4 \
  --host 0.0.0.0 --port 8001 \
  --gpu-memory-utilization 0.60 \
  --max-model-len 131072 \
  --max-num-seqs 4 \
  --max-num-batched-tokens 8192 \
  --kv-cache-dtype fp8 \
  --reasoning-parser qwen3 \
  --tool-call-parser qwen3_xml \
  --enable-auto-tool-choice \
  --speculative-config '{"method":"mtp","num_speculative_tokens":5}'
```

Changes vs the draft command:
- **Port 8001**, matching the deployed `model_bridge` upstream (or keep 8000
  and change `MODEL_BRIDGE_UPSTREAM` in the LattePanda `.env`).
- `--max-model-len 131072` instead of 262144: real prompts are ~15k tokens
  (selected_items avg ~47k chars); halving the window frees KV headroom.
- `--kv-cache-dtype fp8`: native on Blackwell, doubles KV capacity.
- Dropped `--distributed-executor-backend mp` (no-op at TP=1) and
  `--enable-chunked-prefill`/`--enable-prefix-caching` (defaults in vLLM V1).
- `--speculative-config` MTP requires the checkpoint to ship MTP heads; if
  vLLM errors on load, drop the flag.

Point the shadow lane at it (on the LattePanda `.env`):

```
BCN_SHADOW_ENABLED=true
BCN_SHADOW_CANDIDATE_OVERRIDES_PATH=/app/bcn/config/shadow_qwen3_8_27b_bridge.json
```

## 2. Export training data (LattePanda)

```bash
docker exec broken-cloud-news-bcn-1 bcn export-training --output-dir /tmp/training_export
docker cp broken-cloud-news-bcn-1:/tmp/training_export ./training_export
# analyst dataset (edit the prompt stub in the SQL first):
docker exec -i broken-cloud-news-postgres-1 psql -U broken_cloud_news_agent_db \
  -d broken_cloud_news -At -f - < training/export_analyst_sft.sql > training_export/analyst_sft.jsonl
scp -r training_export avkov@192.168.0.9:~/
```

Current volumes (checked 2026-08-18): 222 published / 496 total generation
runs (Mar–Aug 2026), 354 final drafts, 397 auto preference pairs, 256
AI-review edited rewrites, 607 round rewrites, 9,858 analyzed news items,
172 shadow evaluation runs.

## 3. Train (Spark)

Order of attack:
1. **Analyst adapter** — 9.8k examples, structured output, biggest call
   volume (15-min cadence + OpenAI rate-limit pain), lowest risk.
2. **Writer adapter** — only if the zero-shot shadow baseline misses critic
   gates. ~350 SFT rows + optional DPO on ~650 preference pairs.
3. Keep critic/verifier/AI-review on OpenAI initially so evaluation stays
   independent of the model being promoted.

```bash
python training/qlora_sft_unsloth.py --data training_export/analyst_sft.jsonl \
  --base unsloth/Qwen3.8-27B --out analyst_qlora --max-seq-len 8192 --merge
python training/qlora_sft_unsloth.py --data training_export/sft.jsonl \
  --out writer_qlora --max-seq-len 16384 --merge
```

QLoRA cannot start from the NVFP4 serving artifact — train from the BF16
base, then either serve the adapter directly (`vllm serve ... --enable-lora
--lora-modules writer=writer_qlora/adapter`) or merge and re-quantize to
NVFP4 with llm-compressor for full-speed serving.

## 4. Promote

Hold out the newest ~10% of each dataset for eval. Compare on: critic gate
pass rate, score deltas in the shadow lane report, analyst score MAE / tag
overlap vs the GPT labels. Swap roles one at a time via the per-role
`BCN_LLM_*` env overrides; keep the shadow lane running against the OpenAI
champion until several weeks of `promote` recommendations accumulate.
