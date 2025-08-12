FROM ubuntu:22.04

# System deps
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    wget xz-utils python3 python3-pip \
    libxi6 libxxf86vm1 libxrender1 libx11-6 libxrandr2 libxfixes3 libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install Blender (headless)
ENV BLENDER_VERSION=4.1.1
WORKDIR /opt
RUN wget -q https://download.blender.org/release/Blender${BLENDER_VERSION%.*}/blender-${BLENDER_VERSION}-linux-x64.tar.xz \
 && tar -xJf blender-${BLENDER_VERSION}-linux-x64.tar.xz \
 && ln -s /opt/blender-${BLENDER_VERSION}-linux-x64/blender /usr/local/bin/blender \
 && rm blender-${BLENDER_VERSION}-linux-x64.tar.xz

# Verify Blender works at build-time (fail-fast)
RUN blender -v

# App
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . /app/

ENV PORT=8000
CMD gunicorn -b 0.0.0.0:$PORT app:app

