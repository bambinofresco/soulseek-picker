FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/bambinofresco/soulseek-picker" \
      org.opencontainers.image.description="Hand-pick the exact edition of an album from Soulseek; beets tags it, Lidarr and Plex stay in step."

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

# DOWNLOAD_DIR and MUSIC_DIR default to the /mediadata layout in picker/config.py; set them in
# the container template (environment variables make a setting read-only in the web UI).
ENV CONFIG_DIR=/config \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    PYTHONUNBUFFERED=1

EXPOSE 8765
VOLUME ["/config"]
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "picker"]
