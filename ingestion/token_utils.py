from transformers import AutoTokenizer

_tokenizer = AutoTokenizer.from_pretrained('sentence-transformers/all-MiniLM-L6-v2')

def count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text, add_special_tokens=False))

def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Cut text to max_tokens WITHOUT round-tripping through the tokenizer.

    The obvious implementation -- encode, slice ids, decode -- destroys the text.
    all-MiniLM-L6-v2 is an *uncased* model, so decode() returns lowercase with
    punctuation re-spaced: `[ISSUE #100005 - CLOSED]` comes back as
    `[ issue # 100005 - closed ]`. That silently broke two things at once: the
    citation key stopped matching r'#(\d+)' (a space now separates # from the
    digits), and the model was shown mangled lowercase prose to quote from.

    Slicing the ORIGINAL string at a token-aligned character offset preserves the
    text exactly. Offset mapping is what makes that exact rather than a guess.
    """
    enc = _tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    ids = enc['input_ids']
    if len(ids) <= max_tokens:
        return text

    offsets = enc['offset_mapping']
    cut = offsets[max_tokens - 1][1]        # end char of the last kept token
    return text[:cut]

# The embedding model's real limit. all-MiniLM-L6-v2 reports max_seq_length=256 and
# was trained at that length -- anything longer is silently truncated at encode time,
# so a 700-token chunk is embedded from its first 256 tokens and the rest is invisible
# to vector search (while BM25, reading from SQLite, still sees all of it).
# ingestion/chunker.py sizes its budget from this; tests/test_core.py asserts they agree.
EMBED_MAX_TOKENS = 256


def split_by_tokens(text: str, max_tokens: int, overlap: int = 0) -> list[str]:
    """Split text into pieces of at most max_tokens, with token-level overlap.

    Slicing token ids and decoding each slice is exact; slicing characters and hoping
    is not, which is why a single oversized comment previously became a single
    oversized chunk.
    """
    enc = _tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offsets = enc['input_ids'], enc['offset_mapping']
    if len(ids) <= max_tokens:
        return [text]

    step, out = max(1, max_tokens - overlap), []
    for i in range(0, len(ids), step):
        piece = ids[i:i + max_tokens]
        if not piece:
            break
        # Slice the original string, never the decoded tokens -- see truncate_to_tokens.
        start_char = offsets[i][0]
        end_char   = offsets[min(i + max_tokens, len(ids)) - 1][1]
        out.append(text[start_char:end_char])
        if i + max_tokens >= len(ids):
            break
    return out
