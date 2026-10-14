FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends libchromaprint-tools && rm -rf /var/lib/apt/lists/* && fpcalc -v
COPY app.py .
COPY mtl_store.py mtl_safe.py mtl_jobs.py mtl_plugins.py mtl_organize.py mtl_organize_api.py ./
COPY static ./static
EXPOSE 8003
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8003/api/system',timeout=2).read()"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003"]
