# SkillRefiner artifacts

## `20261019`
Run using:
```python
skillrefiner.save_traces_rewards_for_individual_project(
    dataset_path = <...> / 'zlib',
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
Model used = GPT-5.6-terra

Rewards not collected since they haven't been implemented yet.
