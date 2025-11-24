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

# CRISP dependencies
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# CRISP setup.  This comes last because it changes the most often.
WORKDIR /opt/tractor-crisp

COPY pyproject.toml uv.lock ./
COPY crisp/ ./crisp/
RUN uv sync

COPY tools/find_unsafe/Cargo.lock ./tools/find_unsafe/
COPY tools/find_unsafe/Cargo.toml ./tools/find_unsafe/
COPY tools/find_unsafe/src/ ./tools/find_unsafe/src/
RUN cd tools/find_unsafe && cargo build --release
