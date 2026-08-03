#!/bin/bash
set -euo pipefail

models=(
    "glm-5.2:cloud"
    "minimax-m3:cloud"
    "deepseek-v4-pro:cloud"
    "mistral-large-3:675b-cloud"
    "nemotron-3-ultra:cloud"
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
