FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tradezone ./tradezone
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin tradezone \
 && mkdir -p /data && chown tradezone:tradezone /data
COPY deploy/entrypoint.sh /usr/local/bin/tradezone-entrypoint
ENV TZ_HOST=0.0.0.0 TZ_JOURNAL_PATH=/data/tradezone.db
VOLUME /data
EXPOSE 8000
# starts as root only to hand /data to the app user, then runs the app as "tradezone"
ENTRYPOINT ["tradezone-entrypoint"]
CMD ["tradezone", "serve"]
