FROM --platform=linux/arm64 python:3.13-alpine AS upstream
WORKDIR /build
COPY versions.json scripts/fetch_upstream.py ./
RUN test "$(apk --print-arch)" = aarch64 && python fetch_upstream.py /out

FROM --platform=linux/arm64 python:3.13-alpine
RUN test "$(apk --print-arch)" = aarch64 && \
    apk add --no-cache ca-certificates openssl su-exec tini && \
    addgroup -S -g 10001 miho && \
    adduser -S -D -H -u 10001 -G miho miho && \
    mkdir -p /data && chown miho:miho /data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=upstream --chmod=0755 /out/mihomo /usr/local/bin/mihomo
COPY --from=upstream /out/zashboard /opt/zashboard
COPY app ./app
COPY static ./static
COPY versions.json LICENSE ./
COPY licenses /app/licenses
COPY --chmod=0755 scripts/entrypoint.sh /usr/local/bin/miho-entrypoint
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data PORT=8000
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/healthz',timeout=4)"
ENTRYPOINT ["/usr/local/bin/miho-entrypoint"]
CMD ["python", "-m", "app"]

