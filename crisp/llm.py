from dataclasses import dataclass
import json
import os
import pathlib
import re
import requests

from .config import Config, ModelConfig
from .mvir import MVIR, FileNode, TreeNode, LlmOpNode
from .util import ChunkPrinter


API_BASE = os.environ.get('CRISP_API_BASE', 'http://localhost:8080/v1')
# API key to include with requests.  If unset, no API key is included.
API_KEY = os.environ.get('CRISP_API_KEY')
# Model to request from the API.  If unset, the first available model is used.
API_MODEL = os.environ.get('CRISP_API_MODEL')


def emit_file(n: FileNode, path, file_type='Rust'):
    """
    Generate markdown-formatted text giving the contents of file `n`.  Produces
    output of the form:

        /path/to/file.rs
        ```Rust
        // File contents...
        ```
    """
    text = n.body().decode('utf-8')
    return '\n'.join((path, '```' + file_type, text, '```'))

DEFAULT_FILE_TYPE_MAP = {
    '.rs': 'Rust',
    '.c': 'C',
    '.h': 'C',
}

def emit_files(
    mvir: MVIR,
    n: TreeNode,
    glob_filter: str = None,
    file_type_map: dict[str, str] = DEFAULT_FILE_TYPE_MAP,
) -> (str, dict[str, str]):
    """
    Generate markdown-formatted text giving the contents of files in `n`, along
    with a dict mapping short path names used in the output to full paths as
    used in `n`.  Output is formatted like `emit_file`.  If `glob_filter` is
    set to a string, only files whose paths match that glob pattern will be
    included.
    """
    assert isinstance(n, TreeNode)

    if isinstance(glob_filter, str):
        glob_filter = (glob_filter,)

    if len(n.files) == 0:
        common_prefix = ''
    elif len(n.files) == 1:
        common_prefix = os.path.dirname(list(n.files.keys())[0])
    else:
        common_prefix = os.path.commonpath(n.files.keys())

    parts = []
    short_path_map = {}
    for path, child_id in n.files.items():
        if glob_filter is not None:
            path_obj = pathlib.Path(path)
            glob_match = any(path_obj.match(g) for g in glob_filter)
            if not glob_match:
                continue

        short_path = os.path.relpath(path, common_prefix)
        assert short_path not in short_path_map
        short_path_map[short_path] = path

        file_type = file_type_map[os.path.splitext(path)[1]]
        child_node = mvir.node(child_id)
        parts.append(emit_file(child_node, short_path, file_type=file_type))
    return '\n\n'.join(parts), short_path_map

def extract_files(s):
    """
    Extract from `s` all markdown code blocks that appear to match the format
    of `emit_file`.
    """
    files = []

    lines = s.splitlines()
    # `start_i` is the index of the opening line of a markdown code block
    # ("```Rust" or similar), or `None` if we aren't currently in a block.
    start_i = None
    for i, line in enumerate(lines):
        if line == '```':
            if start_i is not None:
                path = lines[start_i - 1]
                text = '\n'.join(lines[start_i + 1 : i]) + '\n'
                files.append((path, text))
            start_i = None
        elif line.startswith('```'):
            start_i = None
            if i == 0:
                continue

            # The line before the start of the block should contain the file
            # path.
            path = lines[i - 1]
            # Some heuristics to reject non-path text before the block.
            if len(path.split(None, 1)) > 1:
                # Invalid path (contains whitespace)
                continue
            if '..' in path:
                continue
            if os.path.normpath(path) != path:
                continue

            file_type = DEFAULT_FILE_TYPE_MAP[os.path.splitext(path)[1]]
            if line.strip().lower() != '```' + file_type.lower():
                continue

            start_i = i

    return files


def sse_events(resp):
    """
    Parse a `requests` streaming response as a sequence of messages in
    Server-Sent Events format.
    """
    # A single message may span multiple lines.  We combine the lines in this
    # accumulator so we can yield the whole message as a unit.
    acc = bytearray()
    for line in resp.iter_lines():
        if len(line) == 0 or line.isspace():
            yield bytes(acc)
            acc.clear()
            continue
        key, sep, value = line.partition(b':')
        # If `sep` is missing, no special handling is required.  "Otherwise,
        # the string is not empty but does not contain a U+003A COLON character
        # (:).  Process the field using the steps described below, using the
        # whole line as the field name, and the empty string as the field
        # value."  Note that this matches the behavior of `partition`.
        if key.lower() != b'data':
            print('unknown SSE key %r in %r' % (key, line))
            continue
        # "Collect the characters on the line after the first U+003A COLON
        # character (:), and let `value` be that string. If `value` starts with
        # a U+0020 SPACE character, remove it from `value`."
        if value.startswith(b' '):
            value = value[1:]
        acc.extend(value)

    # If there are bytes remaining in `acc`, don't yield them.  "If the file
    # ends in the middle of an event, before the final empty line, the
    # incomplete event is not dispatched."

@dataclass
class StreamingMessage:
    """
    Partially-initialized message object.  Fields are initialized incrementally
    by applying deltas as they're received from the server.
    """
    role: str | None = None
    content: str | None = None

    def apply_delta(self, delta):
        if (role := delta.get('role')) is not None:
            assert self.role is None or self.role == role, 'duplicate role'
            self.role = role
        if (content := delta.get('content')) is not None:
            if self.content is None:
                self.content = content
            else:
                self.content += content

@dataclass
class StreamingChoice:
    """
    Partially-initialized choice object.  Fields are initialized incrementally
    by applying deltas as they're received from the server.
    """
    finish_reason: str | None
    message: StreamingMessage

    @staticmethod
    def new():
        return StreamingChoice(finish_reason = None, message = StreamingMessage())

    def apply_delta(self, delta):
        if (finish_reason := delta.get('finish_reason')) is not None:
            assert self.finish_reason is None or self.finish_reason == finish_reason, \
                    'duplicate finish_reason'
            self.finish_reason = finish_reason
        if (message_delta := delta.get('delta')) is not None:
            self.message.apply_delta(message_delta)

def do_request(req, stream=False):
    """
    Send the JSON chat completion request `req` to the default endpoint, and
    return the JSON response.  The request and response text are printed to
    stdout.  If `stream` is set, tokens are printed to stdout as they're
    received, instead of all at once at the end; in either case, the function
    returns only after receiving the entire response.
    """
    p = ChunkPrinter()
    for msg in req['messages']:
        p.end_line()
        p.print(' === %s ===' % msg['role'])
        p.write(msg['content'])

    headers = {}
    if API_KEY is not None:
        headers['Authorization'] = 'Bearer %s' % API_KEY

    if not stream:
        # Non-streaming case is simple.
        resp_dct = requests.post(API_BASE + '/chat/completions',
                json=req, headers=headers).json()

        msg = resp_dct['choices'][0]['message']
        p.set_count(resp_dct['usage']['completion_tokens'])
        p.end_line()
        p.print(' === %s ===' % msg['role'])
        p.write(msg['content'])
        p.finish()

        return resp_dct

    req = req.copy()
    req['stream'] = True
    resp = requests.post(API_BASE + '/chat/completions',
            json=req, headers=headers, stream=True)

    resp_dct = {}
    resp_choices = {}
    for evt in sse_events(resp):
        if evt == b'[DONE]':
            break
        j = json.loads(evt.decode('utf-8'))

        for choice_delta in j.get('choices', ()):
            index = choice_delta['index']
            if (choice := resp_choices.get(index)) is None:
                choice = StreamingChoice.new()
                resp_choices[index] = choice

            prev_role = choice.message.role
            prev_content_len = len(choice.message.content or '')

            choice.apply_delta(choice_delta)

            if index == 0:
                if choice.message.role is not None:
                    if prev_role is None:
                        p.end_line()
                        p.print(' === %s ===' % choice.message.role)
                        if choice.message.content is not None:
                            p.write(choice.message.content)
                    else:
                        content = choice.message.content or ''
                        p.write(content[prev_content_len:])
                    p.flush()
                p.increment()

        for k, v in j.items():
            if k == 'choices':
                continue
            if resp_dct.get(k) is not None:
                continue
            resp_dct[k] = v

    p.finish()

    resp_dct['choices'] = [
            {
                'index': index,
                'finish_reason': choice.finish_reason,
                'message': {
                    'role': choice.message.role,
                    'content': choice.message.content,
                },
            }
            for index, choice in resp_choices.items()]

    return resp_dct

MODEL_REGEX_MULTIPART_SUFFIX = re.compile(r'(.*)-[0-9]{5}-of-[0-9]{5}$')
MODEL_REGEX_QUANT_SUFFIX = re.compile(r'(.*)-(UD-)?(I?Q[0-9]_[A-Z0-9_]*|BF16|FP16)$')

def get_default_model() -> str:
    resp = requests.get(API_BASE + '/models').json()
    name = resp['data'][0]['id']
    name = os.path.basename(name)
    name = os.path.splitext(name)[0]
    if (m := MODEL_REGEX_MULTIPART_SUFFIX.match(name)) is not None:
        name = m.group(1)
    if (m := MODEL_REGEX_QUANT_SUFFIX.match(name)) is not None:
        name = m.group(1)
    return name

def run_rewrite(
        cfg: Config,
        mvir: MVIR,
        prompt_fmt: str,
        input_code: TreeNode,
        *,
        glob_filter: str | list[str] | None = None,
        file_type_map = DEFAULT_FILE_TYPE_MAP,
        format_kwargs: dict = {},
        think: bool = False,
        ) -> TreeNode:
    model = API_MODEL or cfg.model
    if model is None:
        model = get_default_model()
    print('using model %r' % model)
    model_cfg = cfg.models.get(model) or ModelConfig()

    input_files_str, short_path_map = emit_files(mvir, input_code,
        glob_filter=glob_filter, file_type_map=file_type_map)
    prompt = prompt_fmt.format(input_files=input_files_str, **format_kwargs)
    prompt_without_files = prompt_fmt.format(input_files='{input_files}', **format_kwargs)

    req_messages = [
        {'role': 'user', 'content': prompt},
    ]
    prefill = model_cfg.prefill if not think else model_cfg.prefill_think
    if len(prefill) > 0:
        req_messages.append({'role': 'assistant', 'content': prefill})
    req = {
            'messages': req_messages,
            'model': model,
            }
    resp = do_request(req, stream=True)

    output = resp['choices'][0]['message']['content']
    output_files = input_code.files.copy()
    files_changed = 0
    for out_short_path, out_text in extract_files(output):
        assert out_short_path in short_path_map, \
            'output contained unknown file path %r' % (out_short_path,)
        out_path = short_path_map[out_short_path]
        # Note only paths matching `glob_filter` end up in `short_path_map`.
        output_files[out_path] = FileNode.new(mvir, out_text.encode('utf-8')).node_id()
    if output_files == input_code.files:
        print('warning: output contained no files')
        # Proceed.  In the case of `do_main`, this will try again, since there
        # are still unsafety and/or test failures.
    output_code = TreeNode.new(mvir, files=output_files)

    n_op = LlmOpNode.new(
            mvir,
            old_code = input_code.node_id(),
            new_code = output_code.node_id(),
            raw_prompt = FileNode.new(mvir, prompt_without_files).node_id(),
            request = FileNode.new(mvir, json.dumps(req)).node_id(),
            response = FileNode.new(mvir, json.dumps(resp)).node_id(),
            )
    # Record operations and timestamps in the `op_history` reflog.
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

    return output_code, n_op
