from .abc import LLMFileFormat
from . import markdown
from . import xml

_FILE_FORMATTER_CLASSES_BY_NAME = {
    'markdown': markdown.MarkdownFileFormat,
    'xml': xml.XmlFileFormat,
}

def get_file_formatter(name: str, **kwargs) -> LLMFileFormat:
    cls = _FILE_FORMATTER_CLASSES_BY_NAME.get(name)
    if cls is None:
        raise ValueError(f'unknown file formatter {name!r}')
    return cls(**kwargs)
