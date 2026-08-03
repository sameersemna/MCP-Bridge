#!/bin/bash
set -euo pipefail

models=(
    # "openrouter/free"
    # "cohere/north-mini-code:free"
    "inclusionai/ling-3.0-flash:free"
    # "poolside/laguna-xs-2.1:free"
    "poolside/laguna-s-2.1:free"
    "openai/gpt-oss-20b:free"
    # "google/gemma-4-26b-a4b-it:free"
    "google/gemma-4-31b-it:free"
    # "nvidia/nemotron-3-super-120b-a12b:free"
    "nvidia/nemotron-3-ultra-550b-a55b:free"
)

# loop through the models and run test.sh for each one
for model in "${models[@]}"; do
    echo "Running test.sh with model: $model"
    MODEL="$model" bash test.sh
    sleep 2
    echo "Generating PDF report for model: $model"
    bash genpdf.sh
    sleep 2
    echo "PDF report generated for model: $model"
    echo "---------------------------------------------"
done

# "inference_server": {
# "base_url": "https://openrouter.ai/api/v1",
# "api_key": "sk-or-v1-REDACTED"
# },