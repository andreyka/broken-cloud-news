"""QLoRA SFT for broken-cloud-news role adapters. Run on the DGX Spark.

Consumes messages-format JSONL:
  - writer: sft.jsonl from `bcn export-training`
  - analyst: analyst_sft.jsonl from training/export_analyst_sft.sql

Train from the BF16 base (e.g. unsloth/Qwen3.8-27B), NOT the NVFP4 serving
checkpoint -- bitsandbytes QLoRA cannot start from an NVFP4 artifact. After
training, merge to 16-bit and re-quantize with llm-compressor for vLLM.
"""

# unsloth must be imported before trl/transformers/peft for its patches to apply
from unsloth import FastLanguageModel
from unsloth.chat_templates import train_on_responses_only

import argparse
from argparse import BooleanOptionalAction

from datasets import load_dataset
from trl import SFTConfig
from trl import SFTTrainer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="messages-format jsonl")
    parser.add_argument("--base", default="unsloth/Qwen3.8-27B")
    parser.add_argument("--out", default="qlora_out")
    parser.add_argument("--max-seq-len", type=int, default=16384)
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--rank", type=int, default=32)
    parser.add_argument("--merge", action="store_true", help="save merged 16-bit model after training")
    parser.add_argument(
        "--four-bit",
        action=BooleanOptionalAction,
        default=True,
        help="QLoRA on a 4-bit base (default); --no-four-bit = 16-bit LoRA, "
        "needs ~70GB so stop the vLLM container first",
    )
    args = parser.parse_args()

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.base,
        max_seq_length=args.max_seq_len,
        load_in_4bit=args.four_bit,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=args.rank,
        lora_alpha=args.rank * 2,
        lora_dropout=0.0,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        use_gradient_checkpointing="unsloth",
    )

    dataset = load_dataset("json", data_files=args.data, split="train")

    def to_text(row):
        try:
            # Qwen3 hybrid-thinking templates: train plain final answers.
            text = tokenizer.apply_chat_template(
                row["messages"], tokenize=False, enable_thinking=False
            )
        except TypeError:
            text = tokenizer.apply_chat_template(row["messages"], tokenize=False)
        return {"text": text}

    dataset = dataset.map(
        to_text,
        remove_columns=[c for c in dataset.column_names if c != "text"],
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=SFTConfig(
            output_dir=args.out,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=8,
            num_train_epochs=args.epochs,
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_ratio=0.03,
            logging_steps=5,
            save_strategy="epoch",
            bf16=True,
            max_seq_length=args.max_seq_len,
            dataset_text_field="text",
            report_to="none",
        ),
    )
    # Mask loss to assistant turns only (Qwen im_start template markers).
    trainer = train_on_responses_only(
        trainer,
        instruction_part="<|im_start|>user\n",
        response_part="<|im_start|>assistant\n",
    )
    trainer.train()

    model.save_pretrained(args.out + "/adapter")
    tokenizer.save_pretrained(args.out + "/adapter")
    if args.merge:
        model.save_pretrained_merged(
            args.out + "/merged", tokenizer, save_method="merged_16bit"
        )


if __name__ == "__main__":
    main()
