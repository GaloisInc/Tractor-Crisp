# GEPA artifacts
Each folder corresponds to a particular prompt (or set of prompts to be applied together), and their results on different datasets and problems. The `seed_prompt...` folders correspond to seed prompt(s) used to start GEPA optimization. Other folders – generally named starting with a date – are prompt(s) found by GEPA.

Documentation in this README is in reverse chronological order.

---

## Seed prompts agents
Used to start GEPA optimization for agentic workflows.

### `results/zlib_start6604_GPT5p6sol`
- Started with 6604 unsafe. Ran for 20 attempts using GPT-5.6-sol. Final unsafe remaining = 5044.
- Avg unsafe removed per attempt = 78.
- Avg call duration = 199 seconds
- Avg output tokens = 12900
- All attempts pass tests.

### `results/zlib_start622_GPT5p6sol`
- Started with 622 unsafe. Ran for 20 attempts using GPT-5.6-sol. Final unsafe remaining = 525.
- Avg unsafe removed per attempt = 4.85. Barely any unsafe is removed in the last few attempts.
- Avg call duration = 243 seconds
- Avg output tokens = 15224
- All attempts pass tests.

### `results/zlib_start622_GPT5p6terra`
- Started with 622 unsafe. Ran for 20 attempts using GPT-5.6-terra. Final unsafe remaining = 573.
- Avg unsafe removed per attempt = 2.45. Most attempts are stuck at 580 unsafe remaining.
- Avg call duration = 145 seconds
- Avg output tokens = 11955
- All attempts pass tests.


## 20260908[_/B/C]_reflGPT5p6
GEPA optimization done using:
- Seed prompt: `seed_prompts_agents`
- Task LM: CRISP defaults (mostly GPT-5.6-sol)
- Reflection LM: GPT-5.6-sol
- Dataset: `zlib` (only `zlib` in both trainset and valset)
- Max metric calls: 100
- Evaluation function scores:
    ```python
    score_safe = 1
    score_passtests = 0
    score_penalty_per_output_token = 0
    score_penalty_per_call_duration_sec = 0
    ```
- Run on: Godfather

Best prompt found was `20260908_reflGPT5p6`. However, this is only using valset scores. Since trainset and valset are both `zlib`, we inspected `gepa_record.csv` to get a couple of other high performing prompts -- `20260908B_reflGPT5p6` and `20260908C_reflGPT5p6`. Only the ones performing the most unsafe removal are reported below.

### `20260908B_reflGPT5p6/results_zlib_start6604_GPT5p6sol`
- Started with 6604 unsafe. Ran for 20 attempts using GPT-5.6-sol. **Final unsafe remaining = 3683.**
- **Avg unsafe removed per attempt = 146.05. This is 87% improvement over `seed_prompts_agents`.**
- Avg call duration = 453 seconds. This is 127% more than `seed_prompts_agents`.
- Avg output tokens = 28293. This is 119% more than `seed_prompts_agents`.
- All attempts pass tests.

### `20260908_reflGPT5p6/results/zlib_start622_GPT5p6sol`
- Started with 622 unsafe. Ran for 20 attempts using GPT-5.6-sol. Final unsafe remaining = 396.
- **Avg unsafe removed per attempt = 11.3. This is 133% improvement over `seed_prompts_agents`.**
- Avg call duration = 537 seconds. This is 121% more than `seed_prompts_agents`.
- Avg output tokens = 40310. This is 165% more than `seed_prompts_agents`.
- All attempts pass tests.

### `20260908_reflGPT5p6/results/zlib_start622_GPT5p6terra`
- Started with 622 unsafe. Ran for 20 attempts using GPT-5.6-terra. Final unsafe remaining = 477.
- **Avg unsafe removed per attempt = 7.25. This is 196% improvement over `seed_prompts_agents`.**
- Avg call duration = 431 seconds. This is 197% more than `seed_prompts_agents`.
- Avg output tokens = 43760. This is 266% more than `seed_prompts_agents`.
- All attempts pass tests.


## 20260906[_/B/C]_reflGPT5p6
GEPA optimization done using:
- Seed prompt: `seed_prompts_agents`
- Task LM: CRISP defaults (mostly GPT-5.6-sol)
- Reflection LM: GPT-5.6-sol
- Dataset: `zlib` (only `zlib` in both trainset and valset)
- Max metric calls: 100
- Evaluation function scores:
    ```python
    score_safe = 2/3
    score_passtests = 1/3
    score_penalty_per_output_token = 1e-5
    score_penalty_per_call_duration_sec = 1e-3
    ```
- Run on: Godfather

Best prompt found was `20260906_reflGPT5p6`. However, this is only using valset scores. Since trainset and valset are both `zlib`, we inspected `gepa_record.csv` to get a couple of other high performing prompts -- `20260906B_reflGPT5p6` and `20260906C_reflGPT5p6`. Only the ones performing the most unsafe removal are reported below.

### `20260906C_reflGPT5p6/results_zlib_start6604_GPT5p6sol`
- Started with 6604 unsafe. Ran for 20 attempts using GPT-5.6-sol. **Final unsafe remaining = 4070.**
- **Avg unsafe removed per attempt = 126.7. This is 62% improvement over `seed_prompts_agents`.**
- Avg call duration = 310 seconds. This is 56% more than `seed_prompts_agents`.
- Avg output tokens = 23112. This is 79% more than `seed_prompts_agents`.
- All attempts pass tests.

### `20260906C_reflGPT5p6/results/zlib_start622_GPT5p6sol`
- Started with 622 unsafe. Ran for 20 attempts using GPT-5.6-sol. Final unsafe remaining = 436.
- **Avg unsafe removed per attempt = 9.3. This is 92% improvement over `seed_prompts_agents`.**
- Avg call duration = 411 seconds. This is 69% more than `seed_prompts_agents`.
- Avg output tokens = 31687. This is 108% more than `seed_prompts_agents`.
- All attempts pass tests.

### `20260906C_reflGPT5p6/results/zlib_start622_GPT5p6terra`
- Started with 622 unsafe. Ran for 20 attempts using GPT-5.6-terra. Final unsafe remaining = 436.
- **Avg unsafe removed per attempt = 9.3. This is 280% improvement over `seed_prompts_agents`.**
- Avg call duration = 292 seconds. This is 101% more than `seed_prompts_agents`.
- Avg output tokens = 29784. This is 149% more than `seed_prompts_agents`.
- All attempts pass tests.

---

## Seed prompt 2
Used to start GEPA optimization in the updated code setting where the CRISP workflow was used. Here onwards, the evaluation function includes running the T&E-provided tests. Scores are 0 for cannot compile, 0.25 for compiles but doesn't pass tests, 0.5 for compiles and passes tests but is unsafe, and 1 for compiles and passes tests and is safe.

### Results of running prompt with GPT-5.5
| Dataset | Can't compile | Compiles, Tests fail | Compiles, Tests pass, Unsafe | Compiles, Tests pass, Safe | C,TP,S %age |
| -- | -- | -- | -- | -- | -- |
| B01 organic | 6 | 2 | 22 | 8 | 21% |
| B01 synthetic | 20 | 10 | 28 | 27 | 32% |
| B02 organic | 27 | 1 | 14 | 1 | 2% |
| B02 synthetic | 21 | 4 | 9 | 4 | 11% |


## 20260616_taskGPT5p5_reflGPT5p5
GEPA optimization done using the CRISP workflow and running the T&E-provided tests. Scores are 0 for cannot compile, 0.25 for compiles but doesn't pass tests, 0.5 for compiles and passes tests but is unsafe, and 1 for compiles and passes tests and is safe.
- Seed prompt: `seed_prompt_2`
- Task LM: `'openai/gpt-5.5'`
- Reflection LM: `'openai/gpt-5.5'`
- Dataset: `B02_organic`
- Trainset frac: 0.5
- Max metric calls: 150
- Run on: Godfather

### Results of running prompt with GPT-5.5
| Dataset | Can't compile | Compiles, Tests fail | Compiles, Tests pass, Unsafe | Compiles, Tests pass, Safe | C,TP,S %age |
| -- | -- | -- | -- | -- | -- |
| B01 organic | 6 | 5 | 0 | 27 | 71% |
| B01 synthetic | 6 | 10 | 0 | 69 | 81% |
| B02 organic | 21 | 5 | 0 | 17 | 40% |
| B02 synthetic | 16 | 5 | 0 | 17 | 45% |

---

## Seed prompt 1
Used to start GEPA optimization in the older code setting where a) the evaluation function **did not** include running the T&E-provided tests, and b) CRISP workflow was not used. Scores were 0 for cannot compile, 0.5 for compiles & unsafe, 1 for compiles & safe.

### Results of running prompt with GPT-OSS-20b
| Dataset | Can't compile | Compiles & Unsafe | Compiles & Safe | C&S %age |
| -- | -- | -- | -- | -- |
| B01 organic | 16 | 3 | 15 | 44% |
| B01 synthetic | 28 | 10 | 48 | 56% |
| CRUST-Bench | 134 | 15 | 25 | 14% |

### Results of running prompt with GPT-5.4
| Dataset | Can't compile | Compiles & Unsafe | Compiles & Safe | C&S %age |
| -- | -- | -- | -- | -- |
| B01 organic | 22 | 0 | 16 | 42% |
| B01 synthetic | 25 | 5 | 56 | 65% |
| CRUST-Bench | 93 | 19 | 74 | 40% |
| B02 organic | 34 | 1 | 12 | 26% |
| B02 synthetic | 28 | 10 | 27 | 42% |


## 20260326_taskGPT5p4_reflGPT5p4
GEPA optimization done using older code where a) the evaluation function **did not** include running the T&E-provided tests, and b) CRISP workflow was not used. Scores were 0 for cannot compile, 0.5 for compiles & unsafe, 1 for compiles & safe.
- Seed prompt: `seed_prompt_1`
- Task LM: `'openai/gpt-5.4'`
- Reflection LM: `'openai/gpt-5.4'`
- Dataset: `B01_organic`
- Trainset frac: 0.5
- Max metric calls: 150
- Run on: Local Macbook

### Results of running prompt with GPT-5.4
| Dataset | Can't compile | Compiles & Unsafe | Compiles & Safe | C&S %age |
| -- | -- | -- | -- | -- |
| B01 organic | 1 | 0 | 37 | 97% |
| B01 synthetic | 3 | 2 | 81 | 94% |
| CRUST-Bench | 16 | 2 | 168 | 90% |
| B02 organic | 14 | 3 | 30 | 64% |
| B02 synthetic | 3 | 2 | 60 | 92% |


## 20260227_taskGPToss20b_reflGPT5
GEPA optimization done using older code where a) the evaluation function **did not** include running the T&E-provided tests, and b) CRISP workflow was not used. Scores were 0 for cannot compile, 0.5 for compiles & unsafe, 1 for compiles & safe.
- Seed prompt: `seed_prompt_1`
- Task LM: `'ggml-org_gpt-oss-20b-GGUF_gpt-oss-20b-mxfp4.gguf'`
- Reflection LM: `'openai/gpt-5'`
- Dataset: `B01_organic`
- Trainset frac: 0.5
- Max metric calls was 150, but script was interrupted after about 100 metric calls
- Run on: Godfather

### Results of running prompt with GPT-OSS-20b
| Dataset | Can't compile | Compiles & Unsafe | Compiles & Safe | C&S %age |
| -- | -- | -- | -- | -- |
| B01 organic | 10 | 0 | 24 | 71% |
| B01 synthetic | 27 | 0 | 59 | 69% |
| CRUST-Bench | 78 | 0 | 96 | 55% |

---
