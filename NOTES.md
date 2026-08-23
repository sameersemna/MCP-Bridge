docker rm -f local_neo4j
docker run -d \
  --name local_neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password123 \
  -e 'NEO4J_PLUGINS=["apoc","graph-data-science"]' \
  -v neo4j_data:/data \
  neo4j:latest




# Test all MCP servers and their tools list

./test_mcp_tools                          # default http://localhost:11410
./test_mcp_tools --base-url http://host:port --timeout 120

MCP_BRIDGE_URL=http://host:port ./test_mcp_tools./test_mcp_tools # default http://localhost:11410

./test_mcp_tools --base-url http://host:port --timeout 120

MCP_BRIDGE_URL=http://host:port ./test_mcp_tools
