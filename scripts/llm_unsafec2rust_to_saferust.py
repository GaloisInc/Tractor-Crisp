"""
Use LLMs to unsafe Rust to safe Rust.

Pre-requisites:
- Set up environment variables `TRACTOR_OPENAI_API_KEY` and `TRACTOR_ANTHROPIC_API_KEY` to contain the API keys for querying OpenAI and Anthropic models, respectively.
- `pip install anthropic openai`
"""

from anthropic import Anthropic
from openai import OpenAI
import os
from pathlib import Path
import re


############################################################
# Constants
############################################################

UNSAFE_RUST_PROJECTS_FOLDER = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent / 'converted_rust_projects'

SYSTEM_MESSAGE = "You are an expert at converting code from unsafe Rust to safe Rust."
PROMPT = "Convert the following unsafe Rust code within triple backticks to safe Rust.\n\nUnsafe Rust code:\n```rust\n{input_code}```\n\nIn your response, put the safe Rust code within triple backticks as follows:\n{response_start}<Safe Rust code here>{response_end}"


############################################################
# Utilities
############################################################

def get_openai_client() -> OpenAI:
    openai_api_key = os.getenv('TRACTOR_OPENAI_API_KEY')
    assert openai_api_key is not None, "OpenAI API key is not configured. Please create an environment variable called 'TRACTOR_OPENAI_API_KEY' to contain the API key."
    return OpenAI(api_key = openai_api_key)

def get_anthropic_client() -> Anthropic:
    anthropic_api_key = os.getenv("TRACTOR_ANTHROPIC_API_KEY")
    assert anthropic_api_key is not None, "Anthropic API key is not configured. Please create an environment variable called 'TRACTOR_ANTHROPIC_API_KEY' to contain the API key."
    return Anthropic(api_key = anthropic_api_key)

def format_input_code(input_code: str) -> str:
    return input_code + ('' if input_code.endswith('\n') else '\n')


############################################################
# Rewriters
############################################################

def openai_rewriter(
    input_code: str,
    client: OpenAI,
    model: str,
    response_start: str = '```rust\n',
    response_end: str = '\n```',
    **kwargs
) -> str:
    """
    Rewrite code from unsafe Rust to safe Rust using OpenAI LLMs.

    Inputs:
    - input_code: The unsafe Rust code to be rewritten.
    - client: OpenAI client.
    - model: OpenAI model to use.
        For a full list, see https://platform.openai.com/docs/models.
    - response_start, response_end: The model will be instructed to put its output safe Rust code inside these.
        I.e. output should be of the form "...<response_start><output_code><response_end>..."
    - kwargs: Any other argument passed to the model, e.g. `temperature`, `max_output_tokens`, etc.
        For a full list, see https://platform.openai.com/docs/api-reference/responses/create.

    Returns:
    - Rewritten safe Rust code.
        In other words, model response is parsed to extract <output_code> from "...<response_start><output_code><response_end>...", and return.
        If model response is not in the correct format, ValueError is raised.
    """
    response = client.responses.create(
        model = model,
        instructions = SYSTEM_MESSAGE,
        input = PROMPT.format(
            input_code = format_input_code(input_code),
            response_start = response_start,
            response_end = response_end
        ),
        **{k: v for k,v in kwargs.items() if k not in {'model', 'instructions', 'input'}}
    )

    for output in response.output:
        if output.content is None:
            continue
        for content in output.content:
            if content.type != 'output_text':
                continue
            m = re.search(rf'{response_start}(?P<code>.*){response_end}', content.text, flags=re.DOTALL)
            if m:
                return m.group('code')
    raise ValueError(f"Model did not output code in the proper format:\n{response_start}<code>{response_end}")


def anthropic_rewriter(
    input_code: str,
    client: Anthropic,
    model: str,
    max_tokens: int = 10_000,
    response_start: str = '```rust\n',
    response_end: str = '\n```',
    **kwargs
) -> str:
    """
    Rewrite code from unsafe Rust to safe Rust using Anthropic LLMs.

    Inputs:
    - input_code: The unsafe Rust code to be rewritten.
    - client: Anthropic client.
    - model: Anthropic model to use.
        For a full list, see https://platform.claude.com/docs/en/about-claude/models/overview.
    - max_tokens: Max tokens to output. This is required for the Anthropic API.
    - response_start, response_end: The model will be instructed to put its output safe Rust code inside these.
        I.e. output should be of the form "...<response_start><output_code><response_end>..."
    - kwargs: Any other argument passed to the model, e.g. `temperature`, `max_tokens`, etc.
        For a full list, see https://platform.claude.com/docs/en/api/python/messages/create.

    Returns:
    - Rewritten safe Rust code.
        In other words, model response is parsed to extract <output_code> from "...<response_start><output_code><response_end>...", and return.
        If model response is not in the correct format, ValueError is raised.
    """
    response = client.messages.create(
        model = model,
        system = SYSTEM_MESSAGE,
        messages = [
            {
                "role": "user",
                "content": PROMPT.format(
                    input_code = format_input_code(input_code),
                    response_start = response_start,
                    response_end = response_end
                )
            }
        ],
        max_tokens = max_tokens,
        **{k: v for k,v in kwargs.items() if k not in {'model', 'system', 'messages', 'max_tokens'}}
    )

    for content in response.content:
        if content.type != 'text':
            continue
        m = re.search(rf'{response_start}(?P<code>.*){response_end}', content.text, flags=re.DOTALL)
        if m:
            return m.group('code')
    raise ValueError(f"Model did not output code in the proper format:\n{response_start}<code>{response_end}")


############################################################
# Execute
############################################################

if __name__ == "__main__":

    input_file = UNSAFE_RUST_PROJECTS_FOLDER / 'c2rust_Test-Corpus_B01_synthetic/bitfield/src/main.rs'
    with open(input_file, 'r', encoding='utf-8') as f:
        input_code = f.read()

    output_code = anthropic_rewriter(
        input_code = input_code,
        client = get_anthropic_client(),
        model = 'claude-sonnet-4-5'
    )

    output_file = input_file.with_stem(input_file.stem + '_safe')
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(output_code)
