This folder contains prompts found through the GEPA optimization process, i.e. through running `../run_gepa.py`.

Each is named as `<date>_<index>_<task_lm>_<reflection_lm>.txt`.
- `<date>` is yyyymmdd.
- `<index>` is optional, and included if there were multiple promising prompts found on the same day.
- `<task_lm>` is the language model being optimized, e.g. GPT-OSS-20b.
- `<reflection_lm>` is the language model used for reflecting on feedback and suggesting new prompts, e.g. GPT-5.