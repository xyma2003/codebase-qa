# Image digests are supplied and archived by the Linux preparation job.
ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
COPY deploy/runtime.lock /build/runtime.lock
RUN uv venv --python /usr/local/bin/python /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache --require-hashes --torch-backend cpu -r /build/runtime.lock \
    && uv pip check --python /opt/venv/bin/python

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 \
    HF_HOME=/app/.cache/huggingface CODEBASE_QA_LOCAL_MODE=0 PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY ops/os_packages.py /app/ops/os_packages.py
COPY ops/privileged_tools.py /app/ops/privileged_tools.py
COPY deploy/os-security-minimums.json /app/deploy/os-security-minimums.json
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade --no-install-recommends -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
    git ca-certificates apt libapt-pkg7.0 liblzma5 libpcre2-8-0 libssl3t64 openssl tzdata libsqlite3-0 zlib1g \
    && python -m ops.os_packages --output /app/deploy/os-package-build-check.json \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && /usr/local/bin/python -m pip uninstall --yes pip
COPY --from=dependencies /opt/venv /opt/venv
RUN python -m ops.os_packages --output /app/deploy/os-package-check.json
RUN python -m ops.privileged_tools --harden > /app/deploy/privilege-hardening.json \
    && python -m ops.privileged_tools
COPY dependencies/runtime-pins.txt /app/deploy/source-pins.txt
# Compile and archive this Linux CPU lock before building. Never resolve during
# image installation; original Mac baseline remains intact.
COPY deploy/runtime.lock /app/deploy/runtime.lock
COPY deploy/embedding-cache.json /app/deploy/embedding-cache.json
COPY deploy/runtime-layout.json /app/deploy/runtime-layout.json
RUN python -c "import sys; assert sys.version_info[:3] == (3,12,13)"
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
COPY scripts/run_service.py scripts/healthcheck.py scripts/backup_state.py scripts/audit_dependencies.py scripts/manage_service.py scripts/seed_embeddings.py scripts/import_vectors.py /app/scripts/
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
