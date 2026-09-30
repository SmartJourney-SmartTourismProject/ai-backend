# tests/test_chunking.py
# Pure unit tests, no I/O.

from app.rag.chunking import chunk_document, embedding_text


def test_splits_on_wikivoyage_style_headings():
    text = "== Stay safe ==\nWatch for touts.\n\n== Get in ==\nBy train from Colombo."
    chunks = chunk_document(text, "Kandy")
    assert [c.section for c in chunks] == ["Stay safe", "Get in"]
    assert chunks[0].content == "Watch for touts."
    assert chunks[1].content == "By train from Colombo."


def test_splits_on_markdown_style_headings():
    text = "## Visa\nApply for an ETA online.\n\n## Money\nATMs are common in cities."
    chunks = chunk_document(text, "Sri Lanka")
    assert [c.section for c in chunks] == ["Visa", "Money"]


def test_text_before_the_first_heading_is_kept_as_the_lead():
    text = "This is the lead paragraph.\n\n== Understand ==\nMore detail here."
    chunks = chunk_document(text, "Ella")
    assert chunks[0].section is None
    assert chunks[0].content == "This is the lead paragraph."


def test_empty_text_yields_no_chunks():
    assert chunk_document("", "Galle") == []
    assert chunk_document("   \n\n  ", "Galle") == []


def test_long_section_is_packed_into_multiple_chunks_with_overlap():
    # Multiple short paragraphs (real prose), not one giant unbroken block -
    # _pack only splits BETWEEN paragraphs, so packing needs several to work with.
    paragraphs = "\n\n".join(f"Paragraph number {i} about staying safe in the city." for i in range(20))
    text = f"== Stay safe ==\n{paragraphs}"
    chunks = chunk_document(text, "Colombo", max_chars=200, overlap=50)

    assert len(chunks) > 1
    assert all(c.section == "Stay safe" for c in chunks)
    # Overlap: the tail of one chunk reappears at the start of the next -
    # a fact sitting right on the boundary must survive in at least one.
    assert chunks[0].content[-40:] in chunks[1].content


def test_short_paragraphs_are_packed_together_up_to_the_limit():
    text = "== Eat ==\nShort para one.\n\nShort para two.\n\nShort para three."
    chunks = chunk_document(text, "Jaffna", max_chars=1000)
    assert len(chunks) == 1
    assert "Short para one." in chunks[0].content
    assert "Short para three." in chunks[0].content


def test_a_paragraph_longer_than_max_chars_is_kept_whole_not_truncated():
    huge = "x" * 3000
    text = f"== Note ==\n{huge}"
    chunks = chunk_document(text, "Sigiriya", max_chars=500, overlap=50)
    # Not truncated: the full paragraph appears somewhere, uncut.
    assert any(huge in c.content for c in chunks)


def test_chunk_index_is_sequential_across_sections():
    text = "== A ==\nfirst.\n\n== B ==\nsecond."
    chunks = chunk_document(text, "X")
    assert [c.chunk_index for c in chunks] == [0, 1]


def test_embedding_text_includes_breadcrumb_and_section_but_content_stays_clean():
    text = "== Stay safe ==\nWatch your bag on buses."
    [chunk] = chunk_document(text, "Kandy")
    embedded = embedding_text("Kandy", chunk)
    assert embedded == "Kandy › Stay safe: Watch your bag on buses."
    # The stored/quoted content has no breadcrumb prefix baked in.
    assert chunk.content == "Watch your bag on buses."


def test_embedding_text_without_a_section_omits_it():
    [chunk] = chunk_document("Just a lead paragraph.", "Sri Lanka")
    assert embedding_text("Sri Lanka", chunk) == "Sri Lanka: Just a lead paragraph."
