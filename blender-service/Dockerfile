FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive

# 🧱 Install system dependencies and headless Blender
RUN apt-get update && apt-get install -y --no-install-recommends \
    blender python3 python3-pip \
    libgl1 libxi6 libxxf86vm1 libxrender1 libxrandr2 libxfixes3 libxinerama1 \
    libxkbcommon0 libsm6 libice6 libxext6 libxau6 libxdmcp6 libxcb1 libxcb-xfixes0 \
    libegl1 libdrm2 libgbm1 libglu1-mesa \
 && rm -rf /var/lib/apt/lists/*

# ✅ Verify Blender is available
RUN blender -v

# 🔧 Set working directory
WORKDIR /app

# 📦 Copy application code
COPY . /app/

# 🧠 Make sure Blender STL script is available
COPY generate_stl.py /app/

# 🐍 Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# 🌐 Expose port for Render to map correctly
ENV PORT=8000
EXPOSE 8000

# 🚀 Run Flask app using Gunicorn on $PORT provided by Render
CMD gunicorn -b 0.0.0.0:$PORT app:app
