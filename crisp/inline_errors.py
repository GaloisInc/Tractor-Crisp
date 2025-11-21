import sys
import os
import argparse
import re
from pathlib import Path
from collections import defaultdict

def extract_diagnostics(
    json_objects: list[dict],
) -> tuple[dict[str, list[dict]], str]:
    """
    Extract compiler diagnostics from cargo JSON output.

    Args:
        json_objects (list): List of JSON objects from cargo

    Returns:
        tuple: (errors_by_file, stderr_text) where errors_by_file is a dict
               mapping file paths to lists of error information
    """
    errors_by_file = defaultdict(list)
    stderr_lines = []

    for obj in json_objects:
        # Look for compiler messages
        if obj.get('reason') == 'compiler-message':
            message = obj.get('message', {})
            if not message:
                continue

            level = message.get('level', '')
            # Include only errors (not warnings)
            if level != 'error':
                continue

            spans = message.get('spans', [])
            rendered = message.get('rendered', '')
            code = message.get('code', {})
            error_code = code.get('code', '') if code else ''

            # Add to stderr reconstruction
            if rendered:
                stderr_lines.append(rendered)
            error_infos = []
            for span in spans:
                file_path = span.get('file_name', '')
                if not file_path:
                    continue

                line_start = span.get('line_start', 0)
                line_end = span.get('line_end', 0)
                column_start = span.get('column_start', 0)
                column_end = span.get('column_end', 0)

                # Extract highlight positions from text array
                highlight_start = column_start
                highlight_end = column_end
                text_line = None
                label = span.get('label', '')
                text_array = span.get('text', [])
                if text_array and len(text_array) > 0:
                    text_obj = text_array[0]
                    highlight_start = text_obj.get('highlight_start', column_start)
                    highlight_end = text_obj.get('highlight_end', column_end)
                    text_line = text_obj.get('text', None)

                error_info = {
                    'file': file_path,
                    'line': line_start,
                    'label': label,
                    'column': column_start,
                    'message': message.get('message', ''),
                    'error_code': error_code,
                    'level': level,
                    'rendered': rendered,
                    'highlight_start': highlight_start,
                    'highlight_end': highlight_end,
                    'text_obj': text_line
                }
                error_infos.append(error_info)

            errors_by_file[file_path].append(error_infos)

    stderr_text = '\n'.join(stderr_lines)
    return dict(errors_by_file), stderr_text

def insert_inline_error_comments(code: str, errors: list[dict], stderr: str) -> str:
    """
    Insert error comments inline with the code (variant 3 format).

    Args:
        code (str): Source code
        errors (list): List of error dictionaries
        stderr (str): Raw stderr output

    Returns:
        str: Code with inline error comments
    """
    if not errors:
        return code

    lines = code.split('\n')

    # Collect all error locations and their messages
    error_annotations = {}

    # For each error block annotate the error message
    for i, error_t in enumerate(errors):
        if len(error_t) == 0:
            continue
        main_msg = error_t[0]['message']
        line_num = error_t[0]['line']
        if line_num not in error_annotations:
            error_annotations[line_num-1] = []
        error_annotations[line_num-1].append({
            'type': 'main',
            'message': f'ERROR:{i} -- '+main_msg
        })

        for error in error_t:
            line_num = error['line']
            if line_num not in error_annotations:
                error_annotations[line_num] = []

            highlight_start = error.get('highlight_start', error.get('column', 1))
            highlight_end = error.get('highlight_end', error.get('column', 1))
            text_obj = error.get('text_obj', '')
            label = error.get('label', '')
            error_annotations[line_num].append({
                'type': 'sub',
                'message': label,
                'highlight_start': highlight_start,
                'highlight_end': highlight_end,
                'text_obj': text_obj
            })
        error_annotations[error_t[-1]['line']].append({
            'type': 'main',
            'message': f'END_ERROR:{i}'
        })

    # Parse additional context from stderr to find help/note annotations
    stderr_lines = stderr.split('\n')
    for i, sline in enumerate(stderr_lines):
        # Look for help or note messages
        if sline.strip().startswith('help:') or sline.strip().startswith('note:'):
            help_msg = sline.strip()
            # Remove 'help: ' or 'note: ' prefix
            help_msg = re.sub(r'^(help|note):\s*', '', help_msg)

            # Try to find which line this refers to - look backwards for location marker
            for j in range(i-1, max(0, i-10), -1):
                if '-->' in stderr_lines[j]:
                    # FIXME: this should only consider messages about the current file
                    location_match = re.search(r':(\d+):', stderr_lines[j])
                    if location_match:
                        ref_line = int(location_match.group(1))
                        if ref_line not in error_annotations:
                            error_annotations[ref_line] = []
                        error_annotations[ref_line].append({
                            'type': 'help',
                            'message': help_msg
                        })
                        break

    # Insert comments into the code
    result_lines = []
    for i, line in enumerate(lines, 1):
        if i in error_annotations:
            # Get the indentation of the current line
            indent = len(line) - len(line.lstrip())
            indent_str = line[:indent] if indent > 0 else ''

        result_lines.append(line)

        if i in error_annotations:
            # Get the indentation of the current line
            indent = len(line) - len(line.lstrip())
            indent_str = line[:indent] if indent > 0 else ''

            # Add detailed error comments after the line
            annotations = error_annotations[i]
            for ann in annotations:
                if ann['type'] == 'main':
                    result_lines.append(f"{indent_str}// {ann['message']}")
                if ann['type'] == 'sub':
                    # Generate precise caret markers based on highlight positions
                    highlight_start = ann.get('highlight_start', 1)
                    highlight_end = ann.get('highlight_end', 1)
                    text_highlighted = ann.get('text_obj','')
                    # Create spacing and carets
                    # highlight_start is 1-indexed, so we need (highlight_start - 1) spaces
                    num_spaces = max(0, highlight_start - 1)
                    num_carets = max(1, highlight_end - highlight_start)

                    caret_line = f"{indent_str}//{text_highlighted[highlight_start-1:highlight_end-1]} {ann['message']}"
                    result_lines.append(caret_line)
                elif ann['type'] == 'help':
                    result_lines.append(f"{indent_str}// HELP: {ann['message']}")

    return '\n'.join(result_lines)
