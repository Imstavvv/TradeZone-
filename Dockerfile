FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tradezone ./tradezone
RUN pip install --no-cache-dir .
ENV TZ_HOST=0.0.0.0 TZ_JOURNAL_PATH=/data/tradezone.db
VOLUME /data
EXPOSE 8000
CMD ["tradezone", "serve"]
