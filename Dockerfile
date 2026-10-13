FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/app
WORKDIR /app
RUN apk upgrade --no-cache
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip \
    && addgroup -g 10001 -S knowledgeops && adduser -u 10001 -S -G knowledgeops knowledgeops
COPY knowledgeops knowledgeops
COPY migrations migrations
COPY alembic.ini .
COPY data data
COPY web web
COPY schemas schemas
COPY operations operations
COPY scripts/migrate_and_seed.py scripts/migrate_and_seed.py
COPY portfolio-evidence*.json .
COPY contracts.lock.json .
USER 10001:10001
EXPOSE 8044
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request,urllib.parse,os; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8044/api/projects/knowledgeops/ready',headers={'Host':urllib.parse.urlparse(os.getenv('KO_PUBLIC_ORIGIN','http://127.0.0.1:8044')).netloc}),timeout=3)"
CMD ["uvicorn", "knowledgeops.api:app", "--host", "0.0.0.0", "--port", "8044", "--no-access-log", "--limit-concurrency", "40"]

