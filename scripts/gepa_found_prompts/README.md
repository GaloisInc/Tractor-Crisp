This folder contains prompts found through the GEPA optimization process, i.e. through running `../run_gepa.py`.

Notes:
- General format of filenames is `<date in yyyymmdd>_<index, if multiple files were obtained on the same date>_<task lm>_<reflection lm>.txt`. Each filename contains a single system prompt.
- Llama CPP GGUF models are stored locally in `~/Library/Caches/llama.cpp/`.

### `20260209_1_gptoss20b_gpt5.txt`
- Dataset: `Test-Corpus_B01_organic`
- Validation set size: 3
- Task LM: `'ggml-org_gpt-oss-20b-GGUF_gpt-oss-20b-mxfp4.gguf'`
- Reflection LM: `'openai/gpt-5'`
- Max metric calls was 150, but script was interrupted after about 10 metric calls
- Run on: Local Macbook

### `20260209_2_gptoss20b_gpt5.txt`
- Dataset: `Test-Corpus_B01_organic`
- Validation set size: 3
- Task LM: `'ggml-org_gpt-oss-20b-GGUF_gpt-oss-20b-mxfp4.gguf'`
- Reflection LM: `'openai/gpt-5'`
- Max metric calls was 150, but script was interrupted after about 10 metric calls
- Run on: Local Macbook

### `20260227_gptoss20b_gpt5.txt`
- Dataset: `Test-Corpus_B01_organic`
- Trainset frac: 0.5
- Task LM: `'ggml-org_gpt-oss-20b-GGUF_gpt-oss-20b-mxfp4.gguf'`
- Reflection LM: `'openai/gpt-5'`
- Max metric calls was 150, but script was interrupted after about 100 metric calls
- Run on: Godfather
