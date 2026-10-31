# SkillRefiner artifacts


## `20261031_zlib622_GPT5p6t`
- Model used: GPT-5.6-terra
- Traces include agent messages and agent's command execution logs.
- Rewards defined as:
    ```python
    response_evaluator = gepa_agents.ResponseEvaluator(
        score_safe = 1.0 * gepa_common.GEPA_MAX_SCORE,
        score_passtests = 0.0 * gepa_common.GEPA_MAX_SCORE,
        score_penalty_per_output_token = 0.,
        score_penalty_per_call_duration_sec = 0.
    )
    # Get `eval_result` by `__call__()`ing `response_evaluator` with appropriate args from the agent run
    reward = (eval_result.passtests and eval_result.score >= 0.01)
    ```

Run code:
```python
skillrefiner.save_traces_rewards_for_individual_project(
    dataset_path = <...> / 'zlib', # Modified version with 622 unsafe
    starting_node_tag = "current",
    prompt_paths = [
        <...> / f'gepa_artifacts/{prompt_name}/agent_safety_prompt.txt'
        for prompt_name in [
            'seed_prompts_agents',
            '20260906C_reflGPT5p6',
            '20260908_reflGPT5p6',
            '20260908B_reflGPT5p6',
        ]
    ],
    repeats = [20,10,10,10],
)
```


## `20261030_zlib6650_GPT5p6t`
- Model used: GPT-5.6-terra
- Traces include agent messages and agent's command execution logs.
- Rewards defined as:
    ```python
    response_evaluator = gepa_agents.ResponseEvaluator(
        score_safe = 1.0 * gepa_common.GEPA_MAX_SCORE,
        score_passtests = 0.0 * gepa_common.GEPA_MAX_SCORE,
        score_penalty_per_output_token = 0.,
        score_penalty_per_call_duration_sec = 0.
    )
    # Get `eval_result` by `__call__()`ing `response_evaluator` with appropriate args from the agent run
    reward = (eval_result.passtests and eval_result.score >= 0.01)
    ```

Run code:
```python
skillrefiner.save_traces_rewards_for_individual_project(
    dataset_path = <...> / 'zlib', # Original version with 6650 unsafe
    starting_node_tag = "current",
    prompt_paths = [
        <...> / f'gepa_artifacts/{prompt_name}/agent_safety_prompt.txt'
        for prompt_name in [
            'seed_prompts_agents',
            '20260906C_reflGPT5p6',
            '20260908_reflGPT5p6',
            '20260908B_reflGPT5p6',
        ]
    ],
    repeats = [20,10,10,10],
)
```


## `20261019_zlib6604_GPT5p6t`
- Model used: GPT-5.6-terra
- Traces only include agent messages.
- Rewards not collected since they haven't been implemented yet.

Run code:
```python
skillrefiner.save_traces_rewards_for_individual_project(
    dataset_path = <...> / 'zlib', # Original version with ~6.6k unsafe
    starting_node_tag = "current",
    prompt_paths = [
        <...> / f'gepa_artifacts/{prompt_name}/agent_safety_prompt.txt'
        for prompt_name in [
            'seed_prompts_agents',
            '20260906C_reflGPT5p6',
            '20260908_reflGPT5p6',
        ]
    ],
    repeats = 5,
)
```
