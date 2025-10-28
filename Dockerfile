FROM docker.io/rust:bookworm

RUN rustup default 1.87.0

RUN apt-get update

# c2rust deps
RUN apt-get install -y \
    build-essential llvm llvm-dev clang libclang-dev cmake \
    libssl-dev pkg-config python3 git

# Install c2rust
RUN cargo install \
    --locked \
    c2rust

# Install the default toolchain for c2rust transpiled projects
RUN rustup toolchain add \
    -c rustfmt,rustc-dev,rust-src,miri,rust-analyzer \
    nightly-2022-08-08

# Update crates.io index for future use.  There's no dedicated command to force
# an update, but adding a dependency will do it.
# https://stackoverflow.com/a/74708239
RUN mkdir /tmp/empty_project \
    && cd /tmp/empty_project \
    && cargo +nightly-2022-08-08 init \
    && cargo +nightly-2022-08-08 add serde \
    && rm -rf /tmp/empty_project

# Set up sudo so CRISP can use it for sandboxing
RUN apt-get install -y sudo
RUN sed -i -e 's,secure_path=",&/usr/local/cargo/bin:,' /etc/sudoers
RUN echo 'Defaults env_keep+="RUSTUP_HOME"' >>/etc/sudoers

# CRISP sudo-based sandbox configuration
RUN useradd -m crisp_sandbox_user
ENV CRISP_SANDBOX=sudo
ENV CRISP_SANDBOX_SUDO_USER=crisp_sandbox_user

# System packages needed for CRISP
RUN apt-get install -y python3-virtualenv

# CRISP setup.  This comes last because it changes the most often.
WORKDIR /opt/tractor-crisp

COPY requirements.txt ./
RUN virtualenv venv
RUN venv/bin/pip3 install -r requirements.txt

COPY crisp/ ./crisp/
RUN find crisp/ -name __pycache__ | xargs --no-run-if-empty rm -r
# TODO: use `pip3 install -e .` after setting up python project files
ENV PYTHONPATH=/opt/tractor-crisp

COPY tools/find_unsafe/Cargo.lock ./tools/find_unsafe/
COPY tools/find_unsafe/Cargo.toml ./tools/find_unsafe/
COPY tools/find_unsafe/src/ ./tools/find_unsafe/src/
RUN cd tools/find_unsafe && cargo build --release

# Add `/usr/local/bin/crisp` wrapper script
RUN echo '#!/bin/sh' >/usr/local/bin/crisp && \
    echo 'exec /opt/tractor-crisp/venv/bin/python3 -m crisp "$@"' >>/usr/local/bin/crisp && \
    chmod +x /usr/local/bin/crisp
