import os
import requests


API_BASE = os.environ.get('CRISP_API_BASE', 'http://localhost:8080/v1')
# API key to include with requests.  If unset, no API key is included.
API_KEY = os.environ.get('CRISP_API_KEY')
# Model to request from the API.  If unset, the first available model is used.
API_MODEL = os.environ.get('CRISP_API_MODEL')

LLM_FILE_FORMATTER = os.environ.get('CRISP_LLM_FILE_FORMATTER')


MODEL_REGEX_MULTIPART_SUFFIX = re.compile(r'(.*)-[0-9]{5}-of-[0-9]{5}$')
MODEL_REGEX_QUANT_SUFFIX = re.compile(r'(.*)-(UD-)?(I?Q[0-9]_[A-Z0-9_]*|BF16|FP16)$')

def get_default_model() -> str:
    resp = requests.get(API_BASE + '/models').json()
    name = resp['data'][0]['id']
    name = os.path.basename(name)
    name = os.path.splitext(name)[0]
    if (m := MODEL_REGEX_MULTIPART_SUFFIX.match(name)) is not None:
        name = m.group(1)
    if (m := MODEL_REGEX_QUANT_SUFFIX.match(name)) is not None:
        name = m.group(1)
    return name
