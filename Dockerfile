FROM python:3.12-slim

# flac/sox/ffprobe: integrity check and spectrograms. libdiscid0: lets the test suite
# check the disc ID function against libdiscid itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends flac sox ffmpeg libdiscid0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY picker ./picker
RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV CONFIG_DIR=/config \
    DOWNLOAD_DIR=/downloads/soulseek \
    MUSIC_DIR=/music \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    PYTHONUNBUFFERED=1

EXPOSE 8765
VOLUME ["/config"]
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "picker"]
