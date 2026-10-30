import json
from pathlib import Path
import traceback
from typing import Any

from .error import CrispError
from .gepa_common import is_project_gepaready, get_workflow_for_project
from .__main__ import parse_node_id_arg
from .workflow import Workflow


def get_output_text_from_agent_logs(
    agent_logs: list[dict[str, Any]],
    item_separator: str = "\n\n",
) -> str:
    output_text = ""

    for event in agent_logs:
        item = event.get("item")

        if item is None:
            continue

        if isinstance(item, list): # sometimes the dict `item` gets converted to a list of 2-element lists, so we have to convert it back to a dict
            item = dict(item)

        if item.get("type") == "agent_message":
            output_text += (item.get("text", "") + item_separator)
        elif item.get("type") == "command_execution":
            output_text += (item.get("command", "") + item_separator)

    return output_text


def get_single_trace(
    prompt: str,
    workflow: Workflow,
    starting_node_tag: str = "current"
) -> str:
    n_input_code = workflow.mvir.node(parse_node_id_arg(workflow.mvir, starting_node_tag))

    n_c_code = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'c_code'))

    # Try to get pre-saved plans; if they don't exist, ask the agent to generate new ones and save those
    try:
        n_plans = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'plans'))
    except ValueError:
        n_plans = workflow.do_safety_plan_agent(
            n_code = n_input_code,
            n_test_code = n_c_code
        )[1]
        workflow.mvir.set_tag('plans', n_plans.node_id())

    try:
        _, _ = workflow.agent_safety(
            n_code = n_input_code,
            n_test_code = n_c_code,
            n_plans = n_plans,
            agent_safety_prompt = prompt
        )

        n_codex = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'op_history'))
        return get_output_text_from_agent_logs(n_codex.agent_logs)

    except CrispError as e:
        print(f'Safety attempt failed: {e}')
        return f"Safety attempt failed: {e}\n\n{traceback.print_exc()}"


def save_traces_individual_project(
    prompt_paths: list[Path],
    dataset_path: Path,
    repeats: int = 5,
    starting_node_tag: str = "current",
    output_path: Path = Path(__file__).parent.parent / 'skillrefiner_artifacts/traces/new.jsonl'
):
    prompts = [prompt_path.read_text() for prompt_path in prompt_paths]

    assert is_project_gepaready(dataset_path), f"Project at {dataset_path} is not GEPA-ready."
    workflow = get_workflow_for_project(dataset_path)

    for pi,prompt in enumerate(prompts):
        for repeat in range(repeats):
            trace_name = f"trace_{pi}_{repeat}"
            trace = get_single_trace(
                prompt = prompt,
                workflow = workflow,
                starting_node_tag = starting_node_tag
            )
            with open(output_path, 'a', encoding='utf-8') as jsonlfile:
                jsonlfile.write(
                    json.dumps(
                        {
                            "trace_id": trace_name,
                            "spans": [
                                {
                                    "span_id": trace_name+"-s1",
                                    "name": "agent_rollout",
                                    "input_text": prompt,
                                    "output_text": trace.strip(),
                                    "start_time": "",
                                    "end_time": ""
                                }
                            ]
                        }
                    ) + "\n"
                )
