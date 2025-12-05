# TRACTOR CRISP


# Running in Docker

## Building the Docker image

```sh
docker build . -t tractor-crisp
```

## Configuring CRISP

First, copy the example `crisp.toml` config file into your project:

```sh
cp crisp.toml.example /path/to/my-project/crisp.toml
```

Replace `/path/to/my-project` with the path to the C project you want to
translate.  Afterwards, you may want to edit the new config file to customize
it for your project.

## Running CRISP

Run the Docker container:

```sh
docker run --rm -it -v /path/to/my-project:/root/project tractor-crisp
```

Then, within the container, run these commands:

```sh
cd /root/project

# Set up environment variables for accessing OpenAI models
export CRISP_API_BASE=https://api.openai.com/v1
export CRISP_API_KEY=sk-your-api-key-here
export CRISP_API_MODEL=gpt-5-2025-08-07

# As an alternative, you can direct CRISP to connect to llama.cpp or another
# OpenAI-compatible provider running on the host machine:
#export CRISP_API_BASE=http://172.17.0.1:8080/v1

# Import the original files into CRISP and tag them as `c_code`.  This command
# collects all of the `*.c`, `*.h`, and `CMakeLists.txt` files under the
# current directory; if more files are needed to build your C project, edit the
# command accordingly.
find . -name \*.c -o -name \*.h -o -name CMakeLists.txt \
    | xargs crisp commit -t c_code

# Run the CRISP transpiler loop
crisp main

# Export translated Rust from CRISP
crisp checkout
ls rust/
```


# Running outside Docker

CRISP can also run directly on the host machine, using Docker to sandbox
building and testing of the project (to protect against erroneous LLM outputs).

## Setting up the Python virtual environment

CRISP is built and run using the [`uv` tool](https://docs.astral.sh/uv/)

```sh
# Install uv, if needed:
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync
```

## Building the sandbox container

```sh
docker build . -f Dockerfile.work -t tractor-crisp-user
```

## Configuring CRISP

Set up `crisp.toml` as above.

## Running CRISP

Set up a `crisp` alias in your shell:

```
export CRISP_DIR=/path/to/tractor-crisp
alias crisp='uv run --project $CRISP_DIR crisp'
```

Replace `/path/to/tractor-crisp` with the path to your `tractor-crisp`
checkout.

Set up environment variables (`CRISP_API_BASE` etc.) as above.

Follow the same sequence of `crisp commit` + `crisp main` + `crisp checkout` as
above.


# Features

The following features have been implemented in the CRISP transpiler loop:

* Automatic translation of C to unsafe Rust using either c2rust-transpile or
  [Hayroll](https://github.com/UW-HARVEST/Hayroll).  CRISP tries Hayroll first
  and falls back to ordinary c2rust-transpile only if it fails.
* LLM-based safety refactoring.  CRISP uses an LLM to convert unsafe Rust to
  safe Rust.  CRISP checks that the LLM-generated code builds and passes the
  tests before accepting it.
* LLM-based build and test repair.  When code fails to build or doesn't pass
  tests, CRISP invokes an LLM to attempt to fix the problem.  Output is built
  and tested again before accepting it.
* Automatic detection of unsafe code.  The CRISP transpiler loop stops once
  there is no unsafe code left to make safe.
* FFI function splitting.  To preserve ABI compatibility when translating
  libraries, some function signatures must remain unsafe.  To minimize the
  amount of unsafe code, CRISP splits each such function into a small unsafe
  wrapper and a separate implementation function that can then be made safe.
