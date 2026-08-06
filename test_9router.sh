#!/bin/bash
set -euo pipefail

API_KEY="sk-94e4f8c6325c93bf-8kt2c4-ebeef45c"

curl http://zidan:20129/v1/models -H "Authorization: Bearer $API_KEY" | jq '.data[].id'
exit 0

models=(
    "auto/best-free" #
    # "auto/coding:free" #
    "oc/deepseek-v4-flash-free"
    # "oc/mimo-v2.5-free"
    # "oc/hy3-free"
    "oc/nemotron-3-ultra-free"
    "oc/north-mini-code-free"
    # "veoaifree-web/veo"
    # "veo-free/veo"
    # "veoaifree-web/seedance"
    # "veo-free/seedance"
)

# loop through the models and run test.sh for each one
for model in "${models[@]}"; do
    # echo ""
    # echo "---------------------------------------------"
    # echo "---------------------------------------------"
    # echo "Testing (9Router) model: $model"
    # curl http://hp:20128/v1/chat/completions   -H "Authorization: Bearer sk-4fb9197398f0028a-40f3e2-fe53e6d3"   -H "Content-Type: application/json"   -d '{
    #     "model": "'$model'",
    #     "messages": [{"role": "user", "content": "Which Model are you?"}],
    #     "stream": false
    # }'
    # echo ""
    # echo "---------------------------------------------"
    # continue

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
# "base_url": "http://zidan:20129/v1",
# "api_key": "sk-94e4f8c6325c93bf-8kt2c4-ebeef45c"
# },