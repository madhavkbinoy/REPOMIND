from transformers import AutoTokenizer

_tokenizer = AutoTokenizer.from_pretrained('sentence-transformers/all-MiniLM-L6-v2')

def count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text, add_special_tokens=False))

def truncate_to_tokens(text: str, max_tokens: int) -> str:
    ids = _tokenizer.encode(text, add_special_tokens=False)
    if len(ids) <= max_tokens:
        return text
    return _tokenizer.decode(ids[:max_tokens], skip_special_tokens=True)