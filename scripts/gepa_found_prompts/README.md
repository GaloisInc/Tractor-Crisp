This folder contains prompts found through the GEPA optimization process, i.e. through running `../run_gepa.py`.

Notes:
- General format of filenames is `<date in yyyymmdd>_<index, if multiple files were obtained on the same date>_<task lm>_<reflection lm>.txt`. Each filename contains a single system prompt.
- Llama CPP GGUF models are stored locally in `~/Library/Caches/llama.cpp/`.
- Result cells in table contain `cannot compile, compiles & unsafe, compiles & safe`.

## `20260209_1_gptoss20b_gpt5.txt`
- Dataset: `Test-Corpus_B01_organic`
- Validation set size: 3
- Task LM: `'ggml-org_gpt-oss-20b-GGUF_gpt-oss-20b-mxfp4.gguf'`
- Reflection LM: `'openai/gpt-5'`
- Max metric calls was 150, but script was interrupted after about 10 metric calls
- Run on: Local Macbook

## `20260209_2_gptoss20b_gpt5.txt`
- Dataset: `Test-Corpus_B01_organic`
- Validation set size: 3
- Task LM: `'ggml-org_gpt-oss-20b-GGUF_gpt-oss-20b-mxfp4.gguf'`
- Reflection LM: `'openai/gpt-5'`
- Max metric calls was 150, but script was interrupted after about 10 metric calls
- Run on: Local Macbook

## `20260227_gptoss20b_gpt5.txt`
- Dataset: `Test-Corpus_B01_organic`
- Trainset frac: 0.5
- Task LM: `'ggml-org_gpt-oss-20b-GGUF_gpt-oss-20b-mxfp4.gguf'`
- Reflection LM: `'openai/gpt-5'`
- Max metric calls was 150, but script was interrupted after about 100 metric calls
- Run on: Godfather

### Results
| Dataset (#files) | Seed prompt | This prompt |
| -- | -- | -- |
| B01 organic (34) | 16, 3, **15** | 10, 0, **24** |
| B01 synthetic (86) | 28, 10, **48** | 27, 0, **59** |
| CRUST-Bench (174) | 134, 15, **25** | 78, 0, **96** |

## `20260326_gpt5p4_gpt5p4.txt`
- Dataset: `Test-Corpus_B01_organic`
- Trainset frac: 0.5
- Task LM: `'openai/gpt-5.4'`
- Reflection LM: `'openai/gpt-5.4'`
- Max metric calls: 150
- Run on: Local Macbook

### Results
| Dataset (#files) | Seed prompt | This prompt |
| -- | -- | -- |
| B01 organic (38) | 22, 0, **16** | 1, 0, **37** |
| B01 synthetic (86) | 25, 5, **56** | 3, 2, **81** |
| CRUST-Bench (186) | 93, 19, **74** | 16, 2, **168** |
| B02 organic (47) | 34, 1, **12** | 14, 3, **30** |
| B02 synthetic (65) | 28, 10, **27** | 3, 2, **60** |
