#!/bin/bash
set -euo pipefail

API_KEY="sk-94e4f8c6325c93bf-8kt2c4-ebeef45c"

# curl http://zidan:20129/v1/models -H "Authorization: Bearer $API_KEY" | jq '.data[].id'
# exit 0

models=(
    # "free-failsafe-think"
    # "free-deepseek"
    "free-glm"
    "free-nemotron"
)

# loop through the models and run test.sh for each one
for model in "${models[@]}"; do
    # echo ""
    # echo "---------------------------------------------"
    # echo "---------------------------------------------"
    # echo "Testing (9Router) model: $model"
    # curl http://zidan:20129/v1/chat/completions   -H "Authorization: Bearer $API_KEY"   -H "Content-Type: application/json"   -d '{
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

# sudo ss -tulpn | grep :20128
# 9router -p 20129 --no-browser --log --tray &> 9router.log &
