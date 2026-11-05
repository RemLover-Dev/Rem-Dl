# Rems Dl -- headless server image.
# This is the zero-setup option for Linux hosts: everything the app needs
# is inside the image, no Python / WebKitGTK / browser profile setup on the
# host. Native desktop builds (Windows .exe / Linux binary, published with
# GitHub Releases) remain the primary way to run the app as a real desktop
# application; this image is the fallback for servers and minimal distros.
FROM python:3.10-slim

WORKDIR /app

# Docker needs an interface listener inside the container. Publish the host
# port on 127.0.0.1 for SSH-tunnel use; a strong token and host allowlist are
# mandatory before the app will start in this non-loopback container mode.
ENV REMS_HEADLESS=1 \
    PORT=5000 \
    REMS_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

COPY requirements.docker.txt .
RUN pip install --no-cache-dir -r requirements.docker.txt

# Copy application code, web UI and icons. Runtime databases are volume-mounted; see .dockerignore.
COPY . .

# Expose port 5000 for Flask
EXPOSE 5000

# Persist downloads + user data outside the container:
#   docker run -d -p 127.0.0.1:5000:5000 -e REMS_ACCESS_TOKEN=<32+ chars> \
#     -e REMS_ALLOWED_HOSTS=localhost,127.0.0.1 \
#     -v "$(pwd)/Rems Dl:/app/Rems Dl" -v "$(pwd)/database:/app/database" \
#     ghcr.io/remlover-dev/rems-dl:latest
CMD ["python", "Rems_Dl.py"]
