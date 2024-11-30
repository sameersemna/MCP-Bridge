FROM python:3.12-bullseye

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_bridge mcp_bridge

EXPOSE 8000
CMD ["uvicorn", "--app-dir", "mcp_bridge", "main:app", "--host", "0.0.0.0", "--port", "8000"]