import contextlib
import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crisp import llm, prompts
from crisp.mvir import MVIR, FileNode, TreeNode


class RewritePromptTest(unittest.TestCase):
    def test_template_and_legacy_prompts_preserve_code_and_record_without_files(self):
        # Braces in Rust and in formatter instructions must remain literal,
        # including strings that resemble either template language.
        code_text = 'fn main() { println!("{{ input_files }}"); }'
        instructions = 'Return {files} with {{ braces }} and <tags>.'
        formatter = Mock()
        formatter.emit_files.return_value = (code_text, {})
        formatter.get_output_instructions.return_value = instructions
        formatter.get_output_instructions_lowercase.return_value = instructions
        formatter.extract_files.return_value = []
        cfg = SimpleNamespace(models=SimpleNamespace(rewriter='test'), model_options={})
        response = {'choices': [{'message': {'content': ''}}]}
        for prompt in (prompts.llm_safety, prompts.llm_safety_no_ffi,
                       '{output_instructions}\n{input_files}'):
            for separate_system in (False, True):
                with self.subTest(prompt=prompt, separate_system=separate_system):
                    with tempfile.TemporaryDirectory() as tmp:
                        mvir = MVIR(tmp, '.')
                        code = TreeNode.new(mvir, files={})
                        with patch.object(llm.llm_format, 'get_file_formatter', return_value=formatter), \
                                patch.object(llm, 'LLM_FILE_FORMATTER', None), \
                                patch.object(llm, 'do_request', return_value=response) as request, \
                                contextlib.redirect_stdout(io.StringIO()):
                            _, op = llm.run_rewrite(cfg, mvir, prompt, code,
                                separate_system_prompt=separate_system)
                        raw = mvir.node(op.raw_prompt).body_str()
                        self.assertIn('{input_files}', raw)
                        self.assertNotIn(code_text, raw)
                        self.assertIn(instructions, raw)
                        messages = request.call_args.args[0]['messages']
                        if separate_system:
                            self.assertEqual(messages[0]['content'], raw)
                            self.assertEqual(messages[1]['content'], code_text)
                        else:
                            self.assertIn(code_text, messages[0]['content'])
                            self.assertIn(instructions, messages[0]['content'])
