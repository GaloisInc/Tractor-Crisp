# GEPA artifacts
Each folder is the prompt and results for a particular prompt.

Files:
- `prompt.txt` contains the prompt itself. This is meant to be provided as the **system prompt**.
- `results_<dataset>_<model>.csv` contains evaluation results of running the prompt on `<dataset>` using `<model>`.


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


## Seed prompt 2
Used to start GEPA optimization in the newer code setting where the CRISP workflow was used. Here onwards, the evaluation function includes running the T&E-provided tests. Scores are 0 for cannot compile, 0.25 for compiles but doesn't pass tests, 0.5 for compiles and passes tests but is unsafe, and 1 for compiles and passes tests and is safe.

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
