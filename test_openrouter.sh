#!/bin/bash
set -euo pipefail
clear
echo "Start...."
API_KEY="sk-or-v1-REDACTED"

models=()
# models=(
#     "poolside/laguna-xs-2.1:free"
#     "poolside/laguna-s-2.1:free"
#     "cohere/north-mini-code:free"
#     "inclusionai/ling-3.0-flash:free"
#     "openai/gpt-oss-20b:free"
#     # "google/gemma-4-26b-a4b-it:free"
#     # "google/gemma-4-31b-it:free"
#     "nvidia/nemotron-3-super-120b-a12b:free"
#     "nvidia/nemotron-3-ultra-550b-a55b:free"
#     "openrouter/free"
#     "nvidia/nemotron-3-nano-30b-a3b:free"
#     "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
# )

# jq -r '.data[] | select((.id | endswith(":free")) and (.pricing.prompt == "0")) | .id')
openrouter_free_models=$(curl -s -X GET \
  'http://localhost:11410/v1/models' | \
  jq -r '.data[] | select((.id | endswith(":free")) and (.pricing.prompt == "0") and ((.supported_parameters // []) | index("tools") != null) and ((.reasoning.mandatory // false) != true)) | .id')

# get all free models from OpenRouter
# openrouter_free_models=$(curl -s -X GET \
#   'https://openrouter.ai/api/v1/models' \
#   -H "Authorization: Bearer $API_KEY" | \
#   jq '.data[] | select((.id | endswith(":free")) and (.pricing.prompt == "0")) | .id')
echo "OpenRouter free models: ${openrouter_free_models}"
for model in ${openrouter_free_models}; do
    # echo "Adding OpenRouter free model: $model"
    models+=("$model")
done
# exit 0
sleep 5

# loop through the models and run test.sh for each one
for model in "${models[@]}"; do
    echo ""
    echo "---------------------------------------------"
    echo "---------------------------------------------"
    # echo "Testing Open Router model: $model"
    # curl https://openrouter.ai/api/v1/chat/completions   -H "Authorization: Bearer $API_KEY"   -H "Content-Type: application/json"   -d '{
    #     "model": "'$model'",
    #     "messages": [{"role": "user", "content": "Hello!"}]
    # }' | jq
    # echo "---------------------------------------------"
    # continue

    echo "Running test.sh with model: $model"
    MODEL="$model" bash test.sh
    sleep 2
    echo ''
    echo "---------------------------------------------"

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
# "base_url": "https://openrouter.ai/api/v1",
# "api_key": "sk-or-v1-REDACTED"
# },