FROM python:3.12-bullseye

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# install uv to run stdio clients (uvx)
RUN pip install --no-cache-dir uv

# install npx to run stdio clients (npx)
RUN apt-get update && apt-get install -y --no-install-recommends npm

COPY mcp_bridge mcp_bridge

EXPOSE 8000
CMD ["uvicorn", "--app-dir", "mcp_bridge", "main:app", "--host", "0.0.0.0", "--port", "8000"]
