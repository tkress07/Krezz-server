# Krezz-server/blender-service/Dockerfile
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Blender (headless) + Python + GL libs Blender needs
RUN apt-get update && apt-get install -y \
    blender \
    python3 python3-pip \
    libgl1-mesa libx11-6 libxi6 libxrender1 libxfixes3 libxext6 libxrandr2 libxdamage1 libxxf86vm1 libsm6 libice6 \
    && rm -rf /var/lib/apt/lists/*

# Prove Blender runs during build (build will fail if it doesn't)
RUN blender -v

# Minimal Python deps for our tiny Flask app
RUN pip3 install --no-cache-dir flask gunicorn

WORKDIR /opt/app
COPY app.py .

# We’ll later mount a disk at /data for jobs; not needed today
RUN mkdir -p /data
ENV DATA_ROOT=/data

EXPOSE 10000
CMD gunicorn -w 2 -b 0.0.0.0:10000 app:app
