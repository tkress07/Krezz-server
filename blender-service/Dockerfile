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

# App directory
WORKDIR /app

# COPY app files
COPY . /app/

# ✅ Make sure the Blender generation script is copied
COPY generate_stl.py /app/

# Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# Set default port
ENV PORT=8000

# Run server
CMD gunicorn -b 0.0.0.0:$PORT app:app
