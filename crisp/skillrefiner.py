import json
from pathlib import Path
import traceback
from typing import Any

from .error import CrispError
from . import gepa_agents, gepa_common
from .__main__ import parse_node_id_arg
from .workflow import Workflow


def get_trace(
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

    trace = output_text.strip()
    return trace


def get_reward(
    response_evaluator_kwargs: dict[str, Any],
    score_threshold_for_success: float = 0.01
) -> bool:
    response_evaluator = gepa_agents.ResponseEvaluator(
        score_safe = 1.0 * gepa_common.GEPA_MAX_SCORE,
        score_passtests = 0.0 * gepa_common.GEPA_MAX_SCORE,
        score_penalty_per_output_token = 0.,
        score_penalty_per_call_duration_sec = 0.
    )
    eval_result = response_evaluator(**response_evaluator_kwargs)
    reward = (eval_result.passtests and eval_result.score >= score_threshold_for_success)
    return reward


def get_trace_reward_for_single_run(
    *,
    workflow: Workflow,
    starting_node_tag: str = "current",
    prompt: str
) -> tuple[str, bool]:
    """Get trace and reward for a single run, as required by SkillRefiner.

    Inputs:
    - workflow: The CRISP workflow defining the unsafe code.
    - starting_node_tag: The node in the workflow MVIR to use as input.
    - prompt: The prompt for agent safety.

    Returns:
    - Trace (str)
    - Reward (bool)
    """

    # Get relevant nodes
    # This includes pre-saved plans; if they don't exist, generate new ones and save those
    n_input_code = workflow.mvir.node(parse_node_id_arg(workflow.mvir, starting_node_tag))
    n_c_code = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'c_code'))
    try:
        n_plans = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'plans'))
    except ValueError:
        n_plans = workflow.do_safety_plan_agent(n_code = n_input_code, n_test_code = n_c_code)[1]
        workflow.mvir.set_tag('plans', n_plans.node_id())

    # Run agent using prompt
    try:
        n_output_code, _ = workflow.agent_safety(
            n_code = n_input_code,
            n_test_code = n_c_code,
            n_plans = n_plans,
            agent_safety_prompt = prompt
        )
        n_codex = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'op_history'))

        # Get trace and reward
        trace = get_trace(
            agent_logs = n_codex.agent_logs
        )
        reward = get_reward(
            response_evaluator_kwargs = {
                'workflow': workflow,
                'n_output_code': n_output_code,
                'n_input_code': n_input_code,
                'n_c_code': n_c_code,
                'run_details': {
                    'agent_safety_prompt': gepa_agents.AgentRunDetails(
                        call_duration_sec = n_codex.call_duration_sec,
                        output_tokens = n_codex.output_tokens
                    )
                }
            }
        )

    # Get dummy trace and reward if error is encountered
    except CrispError as e:
        trace = f"Safety attempt failed: {e}\n\n{traceback.print_exc()}"
        reward = False

    # Return trace and reward
    return trace, reward


def save_traces_rewards_for_individual_project(
    *,
    dataset_path: Path,
    starting_node_tag: str = "current",
    prompt_paths: list[Path],
    repeats: int | list[int] = 5,
    output_dir: Path = Path(__file__).parent.parent / 'skillrefiner_artifacts/new'
):
    """
    Get traces and rewards over multiple runs on a project.

    Inputs:
    - dataset_path: Path to the project whose workflow will be created and run.
    - starting_node_tag: The node in the workflow MVIR to use as input.
    - prompt_paths: List of text files containing prompts to run.
    - repeats:
        - If an integer, run each prompt for this many times.
        - If a list of integers, size must match size of `prompt_paths`. Run the corresponding prompt that many times.
    - output_dir: Path to output directory where run artifacts will be stored.

    Effect: Saves in `output_dir`:
    - traces.jsonl
    - binary_rewards.json
    """

    # Get workflow
    assert gepa_common.is_project_gepaready(dataset_path), f"Project at {dataset_path} is not ready to get SkillRefiner artifacts from."
    workflow = gepa_common.get_workflow_for_project(dataset_path)

    # Get prompts
    prompts = [prompt_path.read_text() for prompt_path in prompt_paths]

    # Format repeats
    if isinstance(repeats, int):
        repeats = len(prompts) * [repeats]
    assert len(repeats) == len(prompts), f"Expected `prompts` and `repeats` to have same length, instead found length of `prompts` = {len(prompts)}, length of repeats = {len(repeats)}."

    # Create fresh output directory and define filepaths to hold results
    output_dir.mkdir(parents=True, exist_ok=False)
    traces_filepath = output_dir / 'traces.jsonl'
    rewards_filepath = output_dir / 'binary_rewards.json'

    # Since the rewards file updates a single dictionary, we need to create a dummy rewards file
    # which will be read, appended to, and written back with each new reward
    with open(rewards_filepath, 'w', encoding='utf-8') as f:
        json.dump({}, f)

    # Iterate across prompts and repeats
    for prompt_idx, (prompt, repeat) in enumerate(zip(prompts,repeats)):

        # Repeat runs for each prompt
        for repetition_index in range(repeat):

            # Assign trace ID
            trace_id = f"trace_{prompt_idx}_{repetition_index}"

            # Get trace and reward
            trace, reward = get_trace_reward_for_single_run(
                workflow = workflow,
                starting_node_tag = starting_node_tag,
                prompt = prompt,
            )

            # Write trace
            with open(traces_filepath, 'a', encoding='utf-8') as f:
                f.write(
                    json.dumps(
                        {
                            "trace_id": trace_id,
                            "spans": [
                                {
                                    "span_id": trace_id+"_span0",
                                    "name": "agent_rollout",
                                    "input_text": prompt,
                                    "output_text": trace,
                                    "start_time": "",
                                    "end_time": ""
                                }
                            ]
                        },
                        indent = 4,
                    ) + "\n"
                )

            # Read existing reward, append to it, and write it back
            with open(rewards_filepath, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            with open(rewards_filepath, 'w', encoding='utf-8') as f:
                json.dump(
                    existing | {trace_id: reward},
                    f,
                    indent = 4,
                )
