from pathlib import Path

from crisp import gepa_common as mut


def test_get_expected_formatted_blocks_from_seed_candidate():
    seed_candidate = {
        'agent_safety_prompt': (Path(__file__).resolve().parent.parent / 'gepa_artifacts/test/agent_safety_prompt.txt').read_text(),

        'agent_target_goal_field': "Your current target is the `{field_name}` field of `{struct_name}`, along with any similar or closely related fields.  Your goal is to change the field to use a safe type (such as `Box`, `Vec`, or `Rc`) instead of a raw pointer, and to update all uses of it to be as safe as possible (for example, use sites should not cast back to a raw pointer, since that defeats some of the safety checking).".strip(),

        'some_prompt': "here's the number:{number}. Now, follow {instructions} as per your ID: A-{id}B"
    }
    assert mut.get_expected_formatted_blocks_from_seed_candidate(
        seed_candidate = seed_candidate
    ) == {
        'agent_safety_prompt': {'{target_goal}', '{after_refactoring_instruction}', '{cargo_dir_path}'},
        'agent_target_goal_field': {'{field_name}', '{struct_name}'},
        'some_prompt': {'{number}', '{instructions}', '{id}'}
    }


def test_get_bad_prompts_in_candidate():
    expected_formatted_blocks = {
        'agent_safety_prompt': {'{target_goal}', '{after_refactoring_instruction}', '{cargo_dir_path}'},
        'agent_target_goal_field': {'{field_name}', '{struct_name}'},
        'some_prompt': {'{number}', '{instructions}', '{id}'}
    }

    candidate1 = {
        'agent_safety_prompt': (Path(__file__).resolve().parent.parent / 'gepa_artifacts/test/agent_safety_prompt_fine.txt').read_text(),

        'agent_target_goal_field': "hello `{field_name}`",

        'some_prompt': "here's the number:{number}. Now, follow {instructions} as per your ID: A-{id}{B}"
    }
    assert mut.get_bad_prompts_in_candidate(
        candidate = candidate1,
        expected_formatted_blocks = expected_formatted_blocks
    ) == {'agent_target_goal_field', 'some_prompt'}

    candidate2 = {
        'agent_safety_prompt': (Path(__file__).resolve().parent.parent / 'gepa_artifacts/test/agent_safety_prompt_bad.txt').read_text(),

        'agent_target_goal_field': "Your current target is the `{field_name}` field of `{struct_name}`, along with any similar or closely related fields.  Your goal is to change the field to use a safe type (such as `Box`, `Vec`, or `Rc`) instead of a raw pointer, and to update all uses of it to be as safe as possible (for example, use sites should not cast back to a raw pointer, since that defeats some of the safety checking).".strip(),

        'some_prompt': "here's the number:{number}. Now, follow {instructions} as per your ID: A-{id}B"
    }
    assert mut.get_bad_prompts_in_candidate(
        candidate = candidate2,
        expected_formatted_blocks = expected_formatted_blocks
    ) == {'agent_safety_prompt'}

    candidate3 = {
        'agent_safety_prompt': (Path(__file__).resolve().parent.parent / 'gepa_artifacts/test/agent_safety_prompt_fine.txt').read_text(),

        'agent_target_goal_field': "Your current target is the `{field_name}` field of `{struct_name}`, along with any similar or closely related fields.  Your goal is to change the field to use a safe type (such as `Box`, `Vec`, or `Rc`) instead of a raw pointer, and to update all uses of it to be as safe as possible (for example, use sites should not cast back to a raw pointer, since that defeats some of the safety checking).\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\nLoremipsumdolor\n".strip(),

        'some_prompt': "the quick brown fox jumps over the \n\n\n{id}\nand then goes to find{number} of" + "{instructions}"
    }
    assert mut.get_bad_prompts_in_candidate(
        candidate = candidate3,
        expected_formatted_blocks = expected_formatted_blocks
    ) == set()
