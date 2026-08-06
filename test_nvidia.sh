#!/bin/bash
set -euo pipefail
clear
echo "Start...."

models=(
    # "ai21labs/jamba-1.5-large-instruct"
    "deepseek-ai/deepseek-v4-flash"
    "deepseek-ai/deepseek-v4-pro" ##
    # "ibm/granite-3.0-3b-a800m-instruct"
    # "ibm/granite-3.0-8b-instruct"
    # "meta/llama-3.1-70b-instruct"
    # "meta/llama-3.1-8b-instruct"
    # "meta/llama-3.2-1b-instruct"
    # "meta/llama-3.2-3b-instruct"
    # "meta/llama-3.3-70b-instruct" #
    # "meta/llama-guard-4-12b"
    # "meta/llama2-70b"
    # "microsoft/kosmos-2"
    # "microsoft/phi-3.5-moe-instruct"
    # "mistralai/codestral-22b-instruct-v0.1"
    # "mistralai/mistral-7b-instruct-v0.3"
    # "mistralai/mistral-large"
    # "mistralai/mistral-large-2-instruct"
    # "mistralai/mistral-medium-3.5-128b"
    # "mistralai/mistral-nemotron"
    # "mistralai/mixtral-8x22b-v0.1"
    # "moonshotai/kimi-k2.6"
    # "nv-mistralai/mistral-nemo-12b-instruct"
    # "nvidia/ai-synthetic-video-detector"
    # "nvidia/cosmos-reason2-8b"
    # "nvidia/embed-qa-4"
    # "nvidia/ising-calibration-1.5-31b"
    # "nvidia/llama-3.1-nemoguard-8b-content-safety"
    # "nvidia/llama-3.1-nemoguard-8b-topic-control"
    # "nvidia/llama-3.1-nemotron-51b-instruct"
    # "nvidia/llama-3.1-nemotron-70b-instruct"
    # "nvidia/llama-3.1-nemotron-nano-8b-v1"
    # "nvidia/llama-3.1-nemotron-nano-vl-8b-v1"
    # "nvidia/llama-3.1-nemotron-safety-guard-8b-v3"
    # "nvidia/llama-3.1-nemotron-ultra-253b-v1"
    # "nvidia/llama-3.2-nemoretriever-1b-vlm-embed-v1"
    # "nvidia/llama-3.2-nv-embedqa-1b-v1"
    # "nvidia/llama-3.3-nemotron-super-49b-v1"
    "nvidia/llama-3.3-nemotron-super-49b-v1.5" ##
    # "nvidia/llama-nemotron-embed-1b-v2"
    # "nvidia/llama-nemotron-embed-vl-1b-v2"
    # "nvidia/llama3-chatqa-1.5-70b"
    # "nvidia/mistral-nemo-minitron-8b-8k-instruct"
    # "nvidia/nemoretriever-parse"
    # "nvidia/nemotron-3-embed-1b"
    # "nvidia/nemotron-3-nano-30b-a3b"
    # "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    # "nvidia/nemotron-3-super-120b-a12b"
    # "nvidia/nemotron-3-ultra-550b-a55b" #
    # "nvidia/nemotron-3.5-content-safety"
    # "nvidia/nemotron-4-340b-instruct"
    # "nvidia/nemotron-4-340b-reward"
    # "nvidia/nemotron-mini-4b-instruct"
    # "nvidia/nemotron-nano-12b-v2-vl"
    # "nvidia/nemotron-nano-3-30b-a3b"
    # "nvidia/nemotron-parse"
    # "nvidia/neva-22b"
    # "nvidia/nv-embed-v1"
    # "nvidia/nv-embedcode-7b-v1"
    # "nvidia/nv-embedqa-e5-v5"
    # "nvidia/nv-embedqa-mistral-7b-v2"
    # "nvidia/nvclip"
    # "nvidia/nvidia-nemotron-nano-9b-v2"
    # "nvidia/riva-translate-4b-instruct"
    # "nvidia/riva-translate-4b-instruct-v1.1"
    # "nvidia/riva-translate-4b-instruct-v2"
    # "nvidia/vila"
    "openai/gpt-oss-120b" ##
    # "openai/gpt-oss-20b"
    # "stepfun-ai/step-3.7-flash" #
    # "thinkingmachines/inkling" #
    # "writer/palmyra-creative-122b"
    # "writer/palmyra-fin-70b-32k"
    # "writer/palmyra-med-70b"
    # "writer/palmyra-med-70b-32k"
    "z-ai/glm-5.2" ##
    # "zyphra/zamba2-7b-instruct"
    "poolside/laguna-xs-2.1" ##
    "minimaxai/minimax-m3" ##
)

# loop through the models and run test.sh for each one
for model in "${models[@]}"; do
    # echo "---------------------------------------------"
    # echo "Testing NVIDIA model: $model"
    # curl https://integrate.api.nvidia.com/v1/chat/completions   -H "Authorization: Bearer $NVIDIA_API_KEY"   -H "Content-Type: application/json"   -d '{
    #     "model": "'$model'",
    #     "messages": [{"role": "user", "content": "Hello!"}],
    #     "max_tokens": 64
    # }'
    # echo "---------------------------------------------"
    # exit 0

    echo "Running test.sh with model: $model"
    MODEL="$model" bash test.sh
    sleep 2

    # check if response.md exists and is not empty
    if [[ -s response.md ]]; then
        echo "Response.md exists and is not empty for model: $model"
    else
        echo "Response.md does not exist or is empty for model: $model ====> Skipping PDF generation for model: $model"
        continue
    fi

    echo "Generating PDF report for model: $model"
    bash genpdf.sh
    sleep 2
    echo "PDF report generated for model: $model"
    echo "---------------------------------------------"
done

# "inference_server": {
# "base_url": "https://integrate.api.nvidia.com/v1",
# "api_key": "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"
# },