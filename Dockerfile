FROM python:3.12-slim AS builder

WORKDIR /build
ARG UV_VERSION=0.11.30
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir "uv==$UV_VERSION" \
    && uv sync --locked --no-dev --extra ui --no-install-project \
    && rm -rf /root/.cache/uv

FROM python:3.12-slim AS runtime

ARG INSTALL_DESEQ2=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends hisat2 samtools bcftools subread fastqc multiqc \
    && if [ "$INSTALL_DESEQ2" = "1" ]; then \
         apt-get install -y --no-install-recommends r-base r-bioc-deseq2; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
      /usr/local/bin/pip* \
      /usr/local/lib/python3.12/ensurepip \
      /usr/local/lib/python3.12/site-packages/pip \
      /usr/local/lib/python3.12/site-packages/pip-*.dist-info

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY . .

ARG VINA_URL=https://github.com/ccsb-scripps/AutoDock-Vina/releases/download/v1.2.7/vina_1.2.7_linux_x86_64
ARG VINA_SHA256=F31F774F723BBA7BBE6E9D1C47577020EEA9A8DA16424284C043D22593570644

RUN if [ ! -f /app/tools/vina_1.2.7_linux_x86_64 ]; then \
      python -c "import hashlib,urllib.request; p='/app/tools/vina_1.2.7_linux_x86_64'; urllib.request.urlretrieve('$VINA_URL', p); assert hashlib.sha256(open(p,'rb').read()).hexdigest().upper() == '$VINA_SHA256'"; \
    fi \
    && chmod +x /app/tools/vina_1.2.7_linux_x86_64 \
    && mkdir -p /app/output \
    && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "src.fastapi_app:app", "--host", "0.0.0.0", "--port", "8000"]
