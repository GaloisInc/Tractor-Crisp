from openai import OpenAI
import os
from pathlib import Path
import re


UNSAFE_RUST_PROJECTS_FOLDER = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'converted_rust_projects'


def get_openai_client() -> OpenAI:
    openai_api_key = os.getenv('TRACTOR_OPENAI_API_KEY')
    assert openai_api_key is not None, "OpenAI API key is not configured. Please create an environment variable called 'TRACTOR_OPENAI_API_KEY' to contain the API key."
    client = OpenAI(api_key = openai_api_key)
    return client


def convert_unsafe_rust_to_safe_rust(
    client: OpenAI,
    model: str,
    input_file: Path,
    response_start: str = '```rust\n',
    response_end: str = '\n```'
) -> str:
    with open(input_file, 'r', encoding='utf-8') as f:
        code = f.read()
    response = client.responses.create( 
        model = model,
        instructions = "You are an expert at converting code from unsafe Rust to safe Rust.",
        input = f"Convert the following unsafe Rust code within triple backticks to safe Rust.\n\nUnsafe Rust code:\n```rust\n{code + ('' if code.endswith('\n') else '\n')}```\n\nIn your response, put the safe Rust code within triple backticks as follows:\n{response_start}<Safe Rust code here>{response_end}"
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


def use_DEPRECATED_completions_api(client: OpenAI, model: str, code_snippet: str):
    response = client.chat.completions.create( 
        model=model,
        messages=[
            {'role':'system', 'content':'You are a talented coder.'},
            {'role': 'user', 'content': f"What is wrong with the following program?\n{code_snippet}"}
        ],
        temperature=0.0,
        max_tokens=1000,
        top_p=1,
        frequency_penalty=0,
        presence_penalty=0,
        response_format={"type": "text"},
        n=1
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    client = get_openai_client()

    input_file = UNSAFE_RUST_PROJECTS_FOLDER / 'c2rust_Test-Corpus_B01_synthetic/bitfield/src/main.rs'
    code = convert_unsafe_rust_to_safe_rust(
        client = client,
        model = 'gpt-5-mini',
        input_file = input_file
    )
    output_file = input_file.with_stem(input_file.stem + '_safe')
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(code)
