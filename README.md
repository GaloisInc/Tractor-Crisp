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


# Reading the output

`crisp main` produces a large amount of debug output as it works, so it's
often useful to redirect it to a file with a command like `crisp main |& tee log.txt`.
Afterward, `log.txt` will contain many sections that look like this:

```
 ** llm_safety
orig_code = 7984945391f2bd68221a044e93b628bf5e9b9d4fc3a2b2bf4672fa7a992ca24c
<lots of LLM input and output...>
llm_safety result = 287272ddc60c242e0d90231703d9db4c6f3de23ad9ed48a5ba28eb88343ef237
```

This means it ran the `llm_safety` step, which uses an LLM to remove unsafe
operations from the code, with MVIR node `7984945` as the input, and it
produced node `287272d` as the output.  You can view the contents of these
nodes using `crisp show`:

```
$ crisp show 287272d
287272ddc60c242e0d90231703d9db4c6f3de23ad9ed48a5ba28eb88343ef237
{'files': {'Public-Tests/B01_organic/colourblind_lib/translated_rust/Cargo.lock': NodeId(d9aa22603d5bef7c6585bce72f036224872cc0455a3c6d43057adb2a1073bd63),
           'Public-Tests/B01_organic/colourblind_lib/translated_rust/Cargo.toml': NodeId(08f9a3fcebfcfcd0b85dcbf8d61ab0f17db0a934dce0ae725bc5a1561f73bf3b),
           'Public-Tests/B01_organic/colourblind_lib/translated_rust/build.rs': NodeId(be6b68ae15adc9f70e5909edd21a938a5961dfff72e45d5362cd620667d3c2be),
           'Public-Tests/B01_organic/colourblind_lib/translated_rust/lib.rs': NodeId(a7bc0d0d19bc3fbc24f098267f848956664d61f59904dd739921bb4a961d032a),
           'Public-Tests/B01_organic/colourblind_lib/translated_rust/performance.json': NodeId(3b4bb0e3c61b1f7efc572e72e187cdf3ebedc632e33c7015b8d2310df1cba0d2),
           'Public-Tests/B01_organic/colourblind_lib/translated_rust/rust-toolchain.toml': NodeId(9cfc889a3fdc24455c6e1459b2dd72227ba6b8a85ba9bd0fb218b32029907f4f),
           'Public-Tests/B01_organic/colourblind_lib/translated_rust/src/lib.c': NodeId(23a388b2b0fbfb44569bba25bb982427a1591e134df7191d864d0417c6bcea49),
           'Public-Tests/B01_organic/colourblind_lib/translated_rust/src/lib.rs': NodeId(73ac54edd470b727852f1d0b3f5bdeacda8f58cf697ab30080352724e0d44bcb),
           'Public-Tests/B01_organic/colourblind_lib/translated_rust/statistics.json': NodeId(3669bed0888f19e4005742fbd4dc6d4a54cf0b2fd905756a06d20cee7e65a69d)},
 'kind': 'tree'}
---

```
This is a `TreeNode` (`'kind': 'tree'`) containing a number of `FileNode`s.
You can view all the file contents by running `crisp show --files 287272d`, or
view a single file's contents by running `crisp show` on its `NodeId`.  To copy
out the contents of the `TreeNode` to actual files on disk (e.g. so you can try
compiling the code ), use `crisp checkout 287272d --path ./out`; this will
create `./out/Public-Tests/B01_organic/colourblind_lib/translated_rust/Cargo.lock`
and so on.
