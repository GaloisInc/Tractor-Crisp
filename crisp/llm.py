from dataclasses import dataclass
import json
import os
import pathlib
import re
import requests

from . import llm_format
from .config import Config, ModelConfig
from .mvir import MVIR, FileNode, TreeNode, LlmOpNode
from .util import ChunkPrinter


API_BASE = os.environ.get('CRISP_API_BASE', 'http://localhost:8080/v1')
# API key to include with requests.  If unset, no API key is included.
API_KEY = os.environ.get('CRISP_API_KEY')
# Model to request from the API.  If unset, the first available model is used.
API_MODEL = os.environ.get('CRISP_API_MODEL')


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
    reasoning_content: str | None = None

    def apply_delta(self, delta):
        if (role := delta.get('role')) is not None:
            assert self.role is None or self.role == role, 'duplicate role'
            self.role = role
        if (content := delta.get('content')) is not None:
            if self.content is None:
                self.content = content
            else:
                self.content += content
        if (reasoning_content := delta.get('reasoning_content')) is not None:
            self.reasoning_content = (self.reasoning_content or "") + reasoning_content

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
        if msg.get('reasoning_content'):
            p.print(' === %s (reasoning) ===' % msg['role'])
            p.write(msg['reasoning_content'])
        p.print(' === %s ===' % msg['role'])
        p.write(msg['content'])
        p.finish()

        return resp_dct

    req = req.copy()
    req['stream'] = True
    resp = requests.post(API_BASE + '/chat/completions',
            json=req, headers=headers, stream=True)

    current_role = None
    def emit(role: str, msg: str, is_reasoning: bool = False):
        nonlocal current_role
        if msg == '':
            return
        role_ext = f'{role} (reasoning)' if is_reasoning else role
        if role_ext != current_role:
            p.end_line()
            p.print(f' === {role_ext} ===')
            current_role = role_ext
        p.write(msg)

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
            prev_reasoning_content_len = len(choice.message.reasoning_content or '')

            choice.apply_delta(choice_delta)

            if index == 0:
                if choice.message.role is not None:
                    if prev_role is None:
                        if choice.message.reasoning_content is not None:
                            emit(choice.message.role, choice.message.reasoning_content,
                                is_reasoning=True)
                        if choice.message.content is not None:
                            emit(choice.message.role, choice.message.content)
                    else:
                        reasoning_content = choice.message.reasoning_content or ''
                        emit(choice.message.role, reasoning_content[prev_reasoning_content_len:],
                             is_reasoning=True)
                        content = choice.message.content or ''
                        emit(choice.message.role, content[prev_content_len:])
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
        file_mode: str = 'markdown',
        glob_filter: str | list[str] | None = None,
        format_kwargs: dict = {},
        think: bool = False,
        ) -> TreeNode:
    model = API_MODEL or cfg.model
    if model is None:
        model = get_default_model()
    print('using model %r' % model)
    model_cfg = cfg.models.get(model) or ModelConfig()

    input_files_str, short_path_map = llm_format.emit_files(
        mvir, file_mode, input_code, glob_filter=glob_filter)
    prompt = prompt_fmt.format(
        input_files=input_files_str,
        output_instructions=llm_format.get_output_instructions(file_mode),
        output_instructions_lowercase=llm_format.get_output_instructions_lowercase(file_mode),
        **format_kwargs,
    )
    prompt_without_files = prompt_fmt.format(
        input_files='{input_files}',
        output_instructions=llm_format.get_output_instructions(file_mode),
        output_instructions_lowercase=llm_format.get_output_instructions_lowercase(file_mode),
        **format_kwargs,
    )

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
    for out_short_path, out_text in llm_format.extract_files(output, file_mode):
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
