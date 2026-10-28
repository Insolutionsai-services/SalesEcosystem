FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY salescore ./salescore
# State lives in /data (SQLite, trained models, posters) so the container is disposable
ENV DATABASE_URL=sqlite:////data/sales.db MODEL_DIR=/data/models MEDIA_DIR=/data/media PORT=8080
RUN mkdir -p /data/models /data/media
VOLUME /data
EXPOSE 8080
CMD ["sh", "-c", "python -m salescore dev --host 0.0.0.0 --port $PORT --no-browser"]
