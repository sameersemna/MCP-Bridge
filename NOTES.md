docker rm -f local_neo4j
docker run -d \
  --name local_neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password123 \
  -e 'NEO4J_PLUGINS=["apoc","graph-data-science"]' \
  -v neo4j_data:/data \
  neo4j:latest