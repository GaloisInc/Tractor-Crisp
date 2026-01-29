# Need gcc-13 for hayroll.
# Debian bookworm (12) only has gcc-12.
# Debian trixie (13) has gcc-13.
FROM docker.io/rust:trixie

# rust-analyzer (required by hayroll)'s deps require Rust 1.89
RUN rustup default 1.90.0
RUN rustup +1.90.0 component add rustfmt

RUN apt-get update

# c2rust deps
RUN apt-get install -y \
    build-essential llvm llvm-dev clang libclang-dev cmake \
    libssl-dev pkg-config python3 git bear

# Install the toolchain used to build c2rust
RUN rustup toolchain add \
    --component rustfmt,rustc-dev \
    nightly-2022-08-08

# Install the default toolchain for c2rust and hayroll transpiled projects
RUN rustup toolchain add \
    --component rustfmt \
    nightly-2023-04-15

# Update crates.io index for future use.  There's no dedicated command to force
# an update, but adding a dependency will do it.
# https://stackoverflow.com/a/74708239
RUN mkdir /tmp/empty_project \
    && cd /tmp/empty_project \
    && cargo +nightly-2022-08-08 init \
    && cargo +nightly-2022-08-08 add serde \
    && rm -rf /tmp/empty_project

# `uv` is required for building c2rust-refactor
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
RUN uv python install

# Install c2rust
RUN cd /opt \
    && git clone https://github.com/immunant/c2rust --depth 1 \
    && cd c2rust \
    && git fetch --depth 1 origin 4b400b4aab375836a0563d2d0762e60a5fcf1c1a \
    && git checkout FETCH_HEAD
RUN cd /opt/c2rust \
    && uv venv \
    && uv pip install -r scripts/requirements.txt
RUN cd /opt/c2rust \
    && cargo +nightly-2022-08-08 install --locked --path /opt/c2rust/c2rust
RUN cd /opt/c2rust \
    && cargo +nightly-2022-08-08 install --locked --path /opt/c2rust/c2rust-refactor

# Install hayroll
#
# Note that Hayroll's `prerequisites.bash` pins its git dependencies to
# specific tags, so we don't have to worry (much) about ensuring we get the
# right version.
RUN mkdir -p /opt/hayroll \
    && cd /opt/hayroll \
    && git clone https://github.com/UW-HARVEST/Hayroll \
    && cd Hayroll \
    && git checkout a35b1028c16d5784f45cdbcedff81960d8be8899 \
    && ./prerequisites.bash --no-sudo --llvm-version 18 \
    && ./build.bash
RUN ln -s /opt/hayroll/Hayroll/build/hayroll /usr/local/bin/hayroll

# Install CRISP tool binaries
COPY tools/split_ffi_entry_points /opt/crisp-tools/split_ffi_entry_points
RUN cargo install --locked --path /opt/crisp-tools/split_ffi_entry_points


# Set up sudo so CRISP can use it for sandboxing
RUN apt-get install -y sudo
RUN sed -i -e 's,secure_path=",&/usr/local/cargo/bin:,' /etc/sudoers
RUN sed -i -e 's,secure_path=",&/opt/hayroll/Hayroll/build:,' /etc/sudoers
RUN echo 'Defaults env_keep+="RUSTUP_HOME"' >>/etc/sudoers

# CRISP sudo-based sandbox configuration
RUN useradd -m crisp_sandbox_user
ENV CRISP_SANDBOX=sudo
ENV CRISP_SANDBOX_SUDO_USER=crisp_sandbox_user

# CRISP setup.  This comes last because it changes the most often.
WORKDIR /opt/tractor-crisp

COPY pyproject.toml uv.lock ./
COPY crisp/ ./crisp/
RUN uv sync
# FIXME: currently disabled in favor of the wrapper script below.  Some parts
# of CRISP use `os.path.dirname(__file__)` to find related files, but `uv`
# installs CRISP into a different path, so it can no longer find those files.
# We should either fix CRISP to find its files by a more robust method, or else
# commit to using the wrapper script instead of `uv tool install`.
#RUN uv tool install .

# Add `/usr/local/bin/crisp` wrapper script
RUN echo '#!/bin/sh' >/usr/local/bin/crisp && \
    echo 'uv run --project /opt/tractor-crisp crisp "$@"' >>/usr/local/bin/crisp && \
    chmod +x /usr/local/bin/crisp

COPY tools/find_unsafe/Cargo.lock ./tools/find_unsafe/
COPY tools/find_unsafe/Cargo.toml ./tools/find_unsafe/
COPY tools/find_unsafe/src/ ./tools/find_unsafe/src/
RUN cd tools/find_unsafe && cargo build --release
