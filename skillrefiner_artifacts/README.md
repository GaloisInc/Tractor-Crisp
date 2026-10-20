# SkillRefiner artifacts

## Traces

### `20261019.jsonl`
Run using:
```python
crisp.skillrefiner_traces.save_traces_individual_project(
    prompt_paths = [<...> / f'gepa_artifacts/{prompt_name}/agent_safety_prompt.txt' for prompt_name in [
        'seed_prompts_agents',
        '20260906C_reflGPT5p6',
        '20260908_reflGPT5p6',
    ]],
    dataset_path = <...> / 'zlib',
    repeats = 5,
    starting_node_tag = "current"
)
```
Model used = GPT-5.6-terra
