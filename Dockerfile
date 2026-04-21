# Need gcc-13 for hayroll.
# Debian bookworm (12) only has gcc-12.
# Debian trixie (13) has gcc-13.
FROM docker.io/rust:trixie AS tractor-crisp-user

# rust-analyzer 0.0.329 (used by tools/*) requires Rust 1.93 at minimum
# find_unsafe2 requires a specific nightly and rustc-internal components
RUN rustup default nightly-2026-05-11
RUN rustup +nightly-2026-05-11 component add rustfmt rustc-dev rust-src llvm-tools

RUN apt-get update

# c2rust deps
RUN apt-get install -y \
    build-essential llvm llvm-dev clang libclang-dev cmake \
    libssl-dev pkg-config python3 git bear ripgrep jq ninja-build

# Install the toolchain used to build c2rust
RUN rustup toolchain add \
    --component rustfmt,rustc-dev \
    nightly-2022-08-08

# Install the default toolchain for c2rust and hayroll transpiled projects
RUN rustup toolchain add \
    --component rustfmt \
    nightly-2023-04-15

# Enable sparse registry
ENV CARGO_HOME=/usr/local/cargo
RUN mkdir -p $CARGO_HOME
COPY .cargo/config.toml $CARGO_HOME/config.toml

COPY scripts/cargo-docker-clean.sh /usr/local/bin/

# `uv` is required for building `c2rust-refactor` and crisp scripts.
# Make sure things are installed not under `/root/`
# so that they are accessible by other users with `sudo`.
# `uv` installs binaries in `$XDG_BIN_HOME`.
ENV XDG_BIN_HOME=/usr/local/bin
# `uv` installs data (like libraries) in `$XDG_DATA_HOME/uv`.
ENV XDG_DATA_HOME=/usr/local
# We pin the `uv` version because using directories not owned by the user
# may not be supported by `uv` in the future,
# but it works in the current version.
RUN curl -LsSf https://astral.sh/uv/0.9.29/install.sh | sh
RUN uv python install

# Install c2rust
COPY deps/c2rust /opt/c2rust
RUN cd /opt/c2rust \
    && uv venv \
    && uv pip install -r scripts/requirements.txt
# `cd` to resolve the `rust-toolchain.toml`.
RUN cd /opt/c2rust \
    && cargo-docker-clean.sh cargo install --locked --path /opt/c2rust/c2rust
RUN cd /opt/c2rust \
    && cargo-docker-clean.sh cargo install --locked --path c2rust-refactor

# Install hayroll
#
# Note that Hayroll's `prerequisites.bash` pins its git dependencies to
# specific tags, so we don't have to worry (much) about ensuring we get the
# right version.
COPY deps/hayroll/prerequisites.bash /opt/hayroll/
# Trixie's `llvm` defaults to 19 and so that's what `c2rust` is using, too.
RUN cd /opt/hayroll \
    && ./prerequisites.bash --no-sudo --llvm-version 19 \
    && rm -rf dependencies/z3/build/src/ \
    && mv dependencies/Maki/build/lib/libcpp2c.so . \
    && mv dependencies/Maki/build/bin/cpp2c . \
    && rm -rf dependencies/Maki/build/ \
    && mkdir -p dependencies/Maki/build/lib \
    && mkdir -p dependencies/Maki/build/bin \
    && mv libcpp2c.so dependencies/Maki/build/lib/ \
    && mv cpp2c dependencies/Maki/build/bin/

COPY deps/hayroll/ /opt/hayroll/
RUN cd /opt/hayroll \
    && cargo-docker-clean.sh ./build.bash --release \
    && ln -f build/hayroll . \
    && ln -f build/release/reaper . \
    && ln -f build/release/merger . \
    && ln -f build/release/inliner . \
    && ln -f build/release/cleaner . \
    && rm -rf build/
RUN ln -s /opt/hayroll/hayroll /usr/local/bin/hayroll


# Install CRISP tool binaries
COPY tools/ /opt/crisp-tools/
RUN cargo-docker-clean.sh /opt/crisp-tools/install-all.sh

# Install codex-cli
RUN mkdir /opt/codex-cli \
    && cd /opt/codex-cli \
    && codex_url=https://github.com/openai/codex/releases/download/rust-v0.135.0/codex-x86_64-unknown-linux-musl.tar.gz \
    && wget --quiet "$codex_url" \
    && tar -xzf "$(basename "$codex_url")" \
    && ln -s "$PWD/codex-x86_64-unknown-linux-musl" /usr/local/bin/codex \
    && rm "$(basename "$codex_url")"

# Append the location of the Rust binaries to PATH
# since `sh -l` overwrites that variable with the value
# from /etc/profile and Codex uses `bash -lc` a lot
RUN echo "export PATH=$PATH:${CARGO_HOME}/bin" >>/etc/profile


FROM tractor-crisp-user AS tractor-crisp

# Dependencies for `scripts/test_eval.py`
RUN apt-get install -y universal-ctags

# Set up sudo so CRISP can use it for sandboxing
RUN apt-get install -y sudo
RUN sed -i -e "s,secure_path=\",&${CARGO_HOME}/bin:," /etc/sudoers
RUN sed -i -e 's,secure_path=",&/opt/hayroll/build:,' /etc/sudoers
RUN echo 'Defaults env_keep+="RUSTUP_HOME"' >>/etc/sudoers

# CRISP sudo-based sandbox configuration
RUN useradd -m crisp_sandbox_user
ENV CRISP_SANDBOX=sudo
ENV CRISP_SANDBOX_SUDO_USER=crisp_sandbox_user

# Enable sparse registry for the sandbox user
RUN mkdir -v /home/$CRISP_SANDBOX_SUDO_USER/.cargo \
    && cp -v $CARGO_HOME/config.toml /home/$CRISP_SANDBOX_SUDO_USER/.cargo/ \
    && chown -Rv $CRISP_SANDBOX_SUDO_USER:$CRISP_SANDBOX_SUDO_USER /home/$CRISP_SANDBOX_SUDO_USER/.cargo

# CRISP setup.  This comes last because it changes the most often.
WORKDIR /opt/tractor-crisp

COPY pyproject.toml uv.lock ./
COPY crisp/ ./crisp/
COPY scripts/test_eval.py ./scripts/test_eval.py
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
