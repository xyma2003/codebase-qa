# Image digests are supplied and archived by the Linux preparation job.
ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 \
    HF_HOME=/app/.cache/huggingface CODEBASE_QA_LOCAL_MODE=0
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
COPY dependencies/runtime-pins.txt /app/deploy/source-pins.txt
# Compile and archive this Linux CPU lock before building. Never resolve during
# image installation; original Mac baseline remains intact.
COPY deploy/runtime.lock /app/deploy/runtime.lock
COPY deploy/embedding-cache.json /app/deploy/embedding-cache.json
RUN uv pip install --system --no-cache --require-hashes --torch-backend cpu -r deploy/runtime.lock \
    && uv pip uninstall --system pip \
    && uv pip check && python -c "import sys; assert sys.version_info[:3] == (3,12,13)"
COPY app.py config.py /app/
COPY agent /app/agent
COPY indexer /app/indexer
COPY memory /app/memory
COPY security /app/security
COPY tasks /app/tasks
COPY ops /app/ops
COPY evaluation /app/evaluation
COPY pages /app/pages
COPY .streamlit/config.toml /app/.streamlit/config.toml
COPY scripts/run_service.py scripts/healthcheck.py scripts/backup_state.py scripts/audit_dependencies.py scripts/manage_service.py scripts/seed_embeddings.py /app/scripts/
RUN mkdir -p /app/.streamlit /app/chroma_db /app/repos /app/.baseline_runtime/tasks \
    /app/.baseline_runtime/reviews /app/.baseline_runtime/evaluations /app/.cache/huggingface \
    && chown -R 10001:10001 /app
ARG CODEBASE_REVISION
RUN printf '%s\n' "$CODEBASE_REVISION" > /app/deploy/revision
LABEL org.opencontainers.image.revision=${CODEBASE_REVISION}
USER 10001:10001
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "scripts/healthcheck.py"]
ENTRYPOINT ["python", "scripts/run_service.py"]
