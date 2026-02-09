# Broken Cloud News Agent

A fully automated cloud security newsfeed agent that collects, analyzes, and distributes security news. It leverages **n8n** for orchestration, **Postgres** for state management, and **DGX A100** infrastructure for local AI inference (Qwen-VL for analysis, Flux.1 for image generation).

## Architecture

1.  **Collectors**: Independent n8n workflows fetch data from GHSA, CISA, RSS, and Twitter/X.
2.  **Scraper**: A headless browser (Browserless) fetches full content for analysis.
3.  **Analyzer**: Uses **Qwen3-VL-30B** to filter "marketing fluff," summarize technical details, and score "juiciness".
4.  **Distributor**: Generates a daily digest with a custom cover image using **Flux.1-schnell** and publishes it.

---

## 🚀 Installation (Management Node)

This node runs the orchestration and database. It can be a small VPS, LattePanda, or Raspberry Pi 5.

### 1. Requirements
*   Docker & Docker Compose
*   Git

### 2. Setup
```bash
# Clone the repository
git clone https://github.com/broken-cloud-news/broken-cloud-news.git
cd broken-cloud-news

# Start n8n, Postgres, and Browserless
docker-compose up -d
```

### 3. Import Workflows
Access n8n at `http://localhost:5678`. Import the following workflows from the `n8n/` directory:

1.  **Collectors**: `n8n/collectors/*.json`
2.  **Scraper**: `n8n/scrapers/browserless.json`
3.  **Analyzer**: `n8n/analyzers/main_analyzer.json`
4.  **Generator**: `n8n/generators/digest_generator.json`
5.  **Distributor**: `n8n/distributors/daily_digest.json`

> **Note**: After importing `daily_digest.json`, manually add an **"Execute Workflow"** node to link it to the Generator workflow.

---

## 🧠 AI Infrastructure (DGX Node)

These components run on your NVIDIA DGX (or heavy GPU server) to provide offline inference.

### Prerequisites
*   NVIDIA Drivers & CUDA 12.x
*   NVIDIA Container Toolkit installed and configured for Docker.

### 1. Deploy Qwen3-VL (Visual Understanding)
Runs as an OpenAI-compatible API using **vLLM**.

```bash
docker run --rm -it \
    --gpus all \
    --ipc=host \
    --ulimit memlock=-1 --ulimit stack=67108864 \
    --shm-size=32g \
    -p 8000:8000 \
    -e HF_HOME=/root/.cache/huggingface \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    nvcr.io/nvidia/vllm:25.12.post1-py3 \
    vllm serve Qwen/Qwen3-VL-30B-A3B-Instruct-FP8 \
    --host 0.0.0.0 --port 8000 \
    --trust-remote-code \
    --dtype auto \
    --gpu-memory-utilization 0.55 \
    --max-model-len 16384 \
    --limit-mm-per-prompt '{"image":2,"video":0}'
```

*   **Endpoint**: `http://<DGX_IP>:8000/v1`

### 2. Deploy Flux.1-schnell (Image Generation)
Runs **ComfyUI** with a custom-built Docker image.

#### A. Build Custom Image
```bash
# Create Dockerfile
cat <<EOF > Dockerfile
FROM nvcr.io/nvidia/pytorch:25.09-py3
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt
RUN git clone https://github.com/comfyanonymous/ComfyUI.git
WORKDIR /opt/ComfyUI
RUN python -m pip install --upgrade pip
RUN pip install torchsde
# Prevent overwriting NVIDIA torch
RUN sed -i '/^torch/d;/^torchvision/d;/^torchaudio/d' requirements.txt
RUN pip install -r requirements.txt
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
EOF

# Build
docker build -t comfyui:arm64-cuda .
```

#### B. Prepare Models
```bash
mkdir -p ~/comfyui/models/checkpoints ~/comfyui/input ~/comfyui/output

# Download Flux.1-schnell
wget -O ~/comfyui/models/checkpoints/flux1-schnell.safetensors \
  https://huggingface.co/black-forest-labs/FLUX.1-schnell/resolve/main/flux1-schnell.safetensors
```

#### C. Run Flux
```bash
docker run --rm -it \
  --gpus all \
  --ipc=host \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --shm-size=32g \
  -p 8188:8188 \
  -v ~/comfyui/models:/opt/ComfyUI/models \
  -v ~/comfyui/input:/opt/ComfyUI/input \
  -v ~/comfyui/output:/opt/ComfyUI/output \
  comfyui:arm64-cuda
```

*   **Endpoint**: `http://<DGX_IP>:8188`

---

## 🔧 Configuration

Update your **n8n Credentials** and **Workflow Nodes** to point to your DGX IP address for:
*   **LLM Analyze Node**: `http://<DGX_IP>:8000/v1` (Model: `Qwen/Qwen3-VL-30B-A3B-Instruct-FP8`)
*   **Gen Cover Image Node**: `http://<DGX_IP>:8188`
