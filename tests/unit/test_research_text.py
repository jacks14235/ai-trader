from __future__ import annotations

import json

from trader.research.text import alpaca_news_item_text, alpaca_news_text


def test_alpaca_news_item_text_removes_markup_and_non_prose() -> None:
    item = {
        "headline": "Chipmaker <em>raises</em> outlook &amp; guidance",
        "source": "Example Wire",
        "author": "A. Reporter",
        "created_at": "2026-09-23T14:00:00Z",
        "url": "https://example.test/story?id=1&amp;view=full",
        "summary": "Demand is <strong>strong</strong>&nbsp; worldwide. &amp;lt;img&amp;gt;",
        "content": (
            "<article><p>First <b>paragraph</b>.</p>"
            "<figure><img src='huge.jpg'><figcaption>Chart noise</figcaption></figure>"
            "<script>window.noise = '&lt;tag&gt;';</script>"
            "<style>.noise { display: block }</style>"
            "<p>Second&nbsp;paragraph &amp; detail.</p></article>"
        ),
    }

    result = alpaca_news_item_text(item)

    assert result == (
        "Headline: Chipmaker raises outlook & guidance\n"
        "Source: Example Wire\n"
        "Author: A. Reporter\n"
        "Published: 2026-09-23T14:00:00Z\n"
        "URL: https://example.test/story?id=1&view=full\n"
        "Summary: Demand is strong worldwide.\n"
        "Body: First paragraph. Second paragraph & detail."
    )
    assert "<" not in result
    assert "huge.jpg" not in result
    assert "Chart noise" not in result
    assert "window.noise" not in result


def test_alpaca_news_text_omits_empty_summary_and_body() -> None:
    result = alpaca_news_text(
        (
            {
                "headline": "Only a headline",
                "source": "Wire",
                "summary": "  ",
                "content": "<figure><img src='chart.png'></figure>",
            },
        )
    )

    assert result == "Headline: Only a headline\nSource: Wire"
    assert "Summary:" not in result
    assert "Body:" not in result


def test_clean_news_is_smaller_than_raw_provider_json() -> None:
    repeated_markup = "".join(
        f"<p>Result {number}: <strong>revenue grew</strong>&nbsp;year over year.</p>"
        f"<img src='https://cdn.example.test/tracking/{number}.png'>"
        for number in range(40)
    )
    item = {
        "id": 101,
        "headline": "Quarterly update",
        "source": "Wire",
        "author": "Reporter",
        "created_at": "2026-09-23T14:00:00Z",
        "updated_at": "2026-09-23T14:10:00Z",
        "url": "https://example.test/update",
        "summary": "Revenue &amp; margin update",
        "content": repeated_markup,
        "symbols": ["INTC"],
    }
    raw_context_excerpt = json.dumps([item], ensure_ascii=False, sort_keys=True)
    clean_context_excerpt = alpaca_news_text((item,))

    assert len(raw_context_excerpt) == 5057
    assert len(clean_context_excerpt) == 1750
    assert len(clean_context_excerpt) < len(raw_context_excerpt)
    assert "<" not in clean_context_excerpt
