# kobodash server: Debian's Chromium screenshots a Jinja2 page; no browser automation library.
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium python3 python3-jinja2 tzdata ca-certificates fonts-dejavu-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server ./server
# Fetch the checksum-pinned dashboard fonts now so the first render needs no network.
RUN FONT_DIR=/app/fonts python3 -c "import sys; sys.path.insert(0, 'server'); import kobodash; kobodash.ensure_fonts()"
ENV FONT_DIR=/app/fonts HOME=/tmp
USER 65534
EXPOSE 8737
CMD ["python3", "server/kobodash.py"]
