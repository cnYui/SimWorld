# syntax=docker/dockerfile:1
# Build from this folder (the SimWorld repository root):
#   docker build -t emcosim .
#   docker run --rm emcosim run toggle_nominal
#   docker run --rm -p 8765:8765 -e EMCOSIM_APP_TOKEN=dev emcosim serve --host 0.0.0.0 --no-browser
#   docker run --rm -i emcosim mcp                       # MCP over stdio
# Your own project:  docker run --rm -v "$PWD:/work" -w /work emcosim run my_scenario

FROM ubuntu:24.04 AS qemu
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates build-essential ninja-build python3 python3-venv pkg-config \
      libglib2.0-dev libpixman-1-dev libgcrypt20-dev libslirp-dev flex bison \
    && rm -rf /var/lib/apt/lists/*
COPY qemu /src/qemu-patch
# Optional --secret id=proxy_ca,src=<ca.pem> for networks with a TLS-intercepting proxy.
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -s /run/secrets/proxy_ca ]; then export GIT_SSL_CAINFO=/run/secrets/proxy_ca; fi; \
    QEMU_SRC=/src/qemu /src/qemu-patch/build.sh /opt/qemu

FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-venv libglib2.0-0t64 libpixman-1-0 libgcrypt20 libslirp0 ngspice \
      libosmesa6 libgl1 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY --from=qemu /opt/qemu /opt/qemu
COPY qemu /opt/emcosim/qemu
ENV EMCOSIM_QEMU=/opt/qemu/bin/qemu-system-xtensa \
    EMCOSIM_QEMU_ROMS=/opt/qemu/share/qemu \
    MUJOCO_GL=osmesa \
    PYOPENGL_PLATFORM=osmesa
COPY . /opt/emcosim/platform
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -s /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir "/opt/emcosim/platform[step]" "anthropic>=2.8"
WORKDIR /opt/emcosim/platform/examples/switch_robot
EXPOSE 8765
ENTRYPOINT ["emcosim"]
CMD ["--help"]
