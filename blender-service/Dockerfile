FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive

# System deps + Blender (apt pulls a compatible headless build)
RUN apt-get update && apt-get install -y --no-install-recommends \
    blender python3 python3-pip \
    libgl1 libxi6 libxxf86vm1 libxrender1 libxrandr2 libxfixes3 libxinerama1 \
    libxkbcommon0 libsm6 libice6 libxext6 libxau6 libxdmcp6 libxcb1 libxcb-xfixes0 \
    libegl1 libdrm2 libgbm1 libglu1-mesa \
 && rm -rf /var/lib/apt/lists/*

# Sanity check: make sure blender is on PATH during build
RUN blender -v

# App
WORKDIR /app
# Build context is blender-service/, so these files must live in that folder
COPY . /app/
RUN pip3 install --no-cache-dir -r requirements.txt

# Render provides $PORT; bind gunicorn to it
ENV PORT=8000
CMD gunicorn -b 0.0.0.0:$PORT app:app
