# path: Dockerfile
FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive

# 1) System deps + headless Blender
RUN apt-get update && apt-get install -y --no-install-recommends \
    blender python3 python3-pip \
    libgl1 libxi6 libxxf86vm1 libxrender1 libxrandr2 libxfixes3 libxinerama1 \
    libxkbcommon0 libsm6 libice6 libxext6 libxau6 libxdmcp6 libxcb1 libxcb-xfixes0 \
    libegl1 libdrm2 libgbm1 libglu1-mesa \
 && rm -rf /var/lib/apt/lists/*

# Optional sanity check
RUN blender -v

# 2) App
WORKDIR /app

# Copy your whole repo (fine)…
COPY . /app/
# …but ensure /tools is present even if .dockerignore excludes it.
COPY tools/ /app/tools/

# Python deps
RUN pip3 install --no-cache-dir -r requirements.txt

# 3) Environment for the Flask app & Blender call
ENV PORT=8000
ENV BLENDER_BIN=blender
ENV BLENDER_SCRIPT=/app/tools/beard_mold_fix.py
ENV BLENDER_TIMEOUT=180

EXPOSE 8000

# 4) Run server
CMD ["gunicorn","-b","0.0.0.0:8000","app:app","--timeout","180"]
