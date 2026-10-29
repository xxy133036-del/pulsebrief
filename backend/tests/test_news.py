from datetime import timedelta

import pytest
from app.models import Subscription, User
from app.scheduling import utc_naive_now
from app.services import news
from app.services.news import (
    add_article,
    article_payload,
    resolve_candidate_items,
    select_candidates,
    source_content_type,
    strip_source_id_from_summary,
)


@pytest.fixture(autouse=True)
def allow_fixture_sources(monkeypatch):
    monkeypatch.setattr(
        news,
        "rights_cleared_source_ids",
        lambda: {
            "candidate-filter-feed",
            "candidate-scan-feed",
            "preference-eval-feed",
            "normalization-feed",
            "cross-language-feed",
            "easy-mandarin-news",
        },
    )
    monkeypatch.setattr(news, "source_quality_hold_ids", lambda: set())


def test_sources_keep_current_events_and_specialist_articles_distinct():
    assert source_content_type("chinanews-domestic") == "news"
    assert source_content_type("chinanews-world") == "news"
    assert source_content_type("openai-news") == "article"
    assert source_content_type("arxiv-cs-ai") == "article"
    assert source_content_type("eu-horizon-magazine") == "article"
    assert source_content_type("legacy-source") == "article"


def test_horizon_article_payload_preserves_author_and_required_republication_credits(db):
    article = add_article(
        db,
        source_id="eu-horizon-magazine",
        title="Building an early warning system",
        summary="Researchers map critical material supply chains.",
        content="Researchers map critical material supply chains to identify disruptions.",
        url="https://projects.research-and-innovation.ec.europa.eu/en/horizon-magazine/test",
        category="科学与创新专题",
        published_at=utc_naive_now(),
        author="Anthony King",
    )

    payload = article_payload(article)
    item = resolve_candidate_items(
        {
            "items": [
                {
                    "article_id": article.id,
                    "title_zh": "研究团队建立关键材料预警机制",
                    "summary": "研究团队梳理关键材料供应链，以便提前发现可能出现的中断。",
                    "certainty": "来源报道：多项供应链数据仍需持续核实。",
                }
            ]
        },
        [article],
        5,
    )[0]

    assert payload["author"] == "Anthony King"
    assert item["author"] == "Anthony King"
    assert item["license_name"] == "CC BY 4.0"
    assert item["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert item["republish_credit"].startswith("This article was originally published in Horizon")
    assert item["change_notice"]
    assert item["certainty"] == "source_checked"


def test_internal_feed_id_is_removed_from_source_summary():
    summary = "AI 消息要点。（来源: inside-java） 后续说明。"

    assert strip_source_id_from_summary(summary, "inside-java") == "AI 消息要点。 后续说明。"


def test_candidate_limit_applies_after_topic_filter(db):
    user = User(email="candidate-filter@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(user_id=user.id, topics=["AI"], keywords=[])
    db.add(subscription)
    db.flush()

    now = utc_naive_now()
    for index in range(12):
        is_relevant = index >= 10
        add_article(
            db,
            source_id="candidate-filter-feed",
            title=f"{'AI research' if is_relevant else 'Unrelated update'} {index}",
            summary="Recent update with source evidence.",
            content="Recent update with source evidence.",
            url=f"https://example.test/candidate/{index}",
            category="AI" if is_relevant else "other",
            published_at=now - timedelta(minutes=index),
        )

    candidates = select_candidates(db, subscription, demo_mode=False, limit=2)

    assert len(candidates) == 2
    assert all(article.category == "AI" for article in candidates)


def test_candidate_selection_excludes_unlicensed_live_articles(db, monkeypatch):
    monkeypatch.setattr(news, "rights_cleared_source_ids", lambda: set())
    user = User(email="rights-filter@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(user_id=user.id, topics=["AI"], keywords=[])
    db.add(subscription)
    db.flush()
    article = add_article(
        db,
        source_id="uncleared-feed",
        title="AI event with factual evidence",
        summary="The source published details about an AI event.",
        content="The source published details about an AI event.",
        url="https://example.test/uncleared",
        category="AI",
        published_at=utc_naive_now(),
    )

    assert select_candidates(db, subscription, demo_mode=False, limit=10) == []
    assert article.source_id == "uncleared-feed"


def test_candidate_selection_excludes_source_on_documented_accuracy_hold(db, monkeypatch):
    monkeypatch.setattr(news, "source_quality_hold_ids", lambda: {"easy-mandarin-news"})
    user = User(email="quality-hold-filter@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(user_id=user.id, topics=["台风"], keywords=[])
    db.add(subscription)
    db.flush()
    article = add_article(
        db,
        source_id="easy-mandarin-news",
        title="台风逼近：中国东南沿海预警",
        summary="气象消息称台风将影响沿海地区。",
        content="气象消息称台风将影响沿海地区。",
        url="https://easymandarin.news/en/articles/example",
        category="综合新闻（AI 二次整理）",
        published_at=utc_naive_now(),
    )

    assert select_candidates(db, subscription, demo_mode=False, limit=10) == []
    assert article.source_id == "easy-mandarin-news"


def test_candidate_scan_finds_relevant_article_after_first_300(db):
    user = User(email="candidate-scan-depth@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(
        user_id=user.id,
        topics=["secret detection"],
        keywords=["GitHub secret scanning"],
    )
    db.add(subscription)
    db.flush()

    now = utc_naive_now()
    for index in range(305):
        add_article(
            db,
            source_id="candidate-scan-feed",
            title=f"Unrelated research update {index}",
            summary="A recent article unrelated to this subscription.",
            content="A recent article unrelated to this subscription.",
            url=f"https://example.test/candidate-scan/{index}",
            category="other",
            published_at=now - timedelta(minutes=index),
        )
    match = add_article(
        db,
        source_id="candidate-scan-feed",
        title="GitHub model for secret detection",
        summary="GitHub secret scanning uses a context-aware classifier.",
        content="The model improves secret detection for GitHub secret scanning.",
        url="https://example.test/candidate-scan/match",
        category="developer-tools",
        published_at=now - timedelta(minutes=400),
    )

    candidates = select_candidates(db, subscription, demo_mode=False, limit=5)

    assert [article.id for article in candidates] == [match.id]


def test_preference_evaluation_handles_any_all_topics_and_exclusions(db):
    user = User(email="preference-eval@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(
        user_id=user.id,
        topics=["AI"],
        keywords=["Qwen", "OpenAI"],
        excluded_keywords=["hiring"],
        match_all=False,
    )
    db.add(subscription)
    db.flush()

    now = utc_naive_now()
    rows = [
        ("AI Qwen model update", "research"),
        ("AI OpenAI model update", "research"),
        ("AI Qwen OpenAI hiring", "research"),
        ("Qwen OpenAI model update", "AI"),
    ]
    ids = []
    for index, (title, category) in enumerate(rows):
        article = add_article(
            db,
            source_id="preference-eval-feed",
            title=title,
            summary="Source-backed release details.",
            content="Source-backed release details.",
            url=f"https://example.test/preference/{index}",
            category=category,
            published_at=now - timedelta(minutes=index),
        )
        ids.append(article.id)

    any_match = select_candidates(db, subscription, demo_mode=False, limit=10)
    subscription.match_all = True
    all_match = select_candidates(db, subscription, demo_mode=False, limit=10)

    assert {article.id for article in any_match} == {ids[0], ids[1], ids[3]}
    assert {article.id for article in all_match} == {ids[3]}


def test_preference_matching_normalizes_case_full_width_and_whitespace(db):
    user = User(email="normalization-eval@example.test")
    db.add(user)
    db.flush()
    subscription = Subscription(
        user_id=user.id,
        topics=["ＡＩ"],
        keywords=["Ｑｗｅｎ　　Copilot"],
        excluded_keywords=["ＨＩＲＩＮＧ"],
        match_all=True,
    )
    db.add(subscription)
    db.flush()

    now = utc_naive_now()
    matching = add_article(
        db,
        source_id="normalization-feed",
        title="AI: Qwen Copilot instructions",
        summary="Official instructions for an AI coding assistant.",
        content="Qwen and Copilot integration instructions.",
        url="https://example.test/normalization/allow",
        category="Developer tools",
        published_at=now,
    )
    add_article(
        db,
        source_id="normalization-feed",
        title="AI: Qwen Copilot hiring update",
        summary="A hiring announcement.",
        content="The team is hiring.",
        url="https://example.test/normalization/exclude",
        category="Developer tools",
        published_at=now - timedelta(minutes=1),
    )

    candidates = select_candidates(db, subscription, demo_mode=False, limit=10)

    assert [article.id for article in candidates] == [matching.id]


def test_chinese_ai_preferences_match_common_english_source_terms(db):
    user = User(email="cross-language-preference@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(user_id=user.id, topics=["大模型"], keywords=["大模型"])
    db.add(subscription)
    db.flush()

    matching = add_article(
        db,
        source_id="cross-language-feed",
        title="How Itaú is building generative AI for its entire customer base",
        summary="A bank applies generative AI to customers and employees.",
        content="A source-backed generative AI deployment story.",
        url="https://example.test/cross-language/match",
        category="large-models",
        published_at=utc_naive_now(),
    )
    add_article(
        db,
        source_id="cross-language-feed",
        title="Java conference schedule",
        summary="Conference dates and sessions.",
        content="A Java developer event.",
        url="https://example.test/cross-language/no-match",
        category="java-development",
        published_at=utc_naive_now() - timedelta(minutes=1),
    )

    candidates = select_candidates(db, subscription, demo_mode=False, limit=10)

    assert [article.id for article in candidates] == [matching.id]


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("财经", "financial"),
        ("社会", "community"),
        ("体育", "athletics"),
        ("全民健身", "physical activity"),
        ("外交", "diplomatic"),
        ("经济", "economic"),
        ("就业", "workforce"),
        ("教育", "educational"),
        ("法治", "judicial"),
        ("科研", "scientific research"),
    ],
)
def test_general_chinese_topics_expand_to_english_news_terms(term, expected):
    assert expected in news.term_variants(term)


def test_chinese_domestic_governance_preference_matches_english_news(db):
    user = User(email="domestic-governance@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(
        user_id=user.id,
        topics=["国内"],
        keywords=["地方", "政务"],
        match_all=False,
    )
    db.add(subscription)
    db.flush()

    matching = add_article(
        db,
        source_id="cross-language-feed",
        title="China's local government expands public services",
        summary="A regional administration announced additional citizen services.",
        content="The municipal government said the program will reach more residents.",
        url="https://example.test/cross-language/domestic-governance",
        category="国际政治",
        published_at=utc_naive_now(),
    )
    unrelated = add_article(
        db,
        source_id="cross-language-feed",
        title="European government expands public services",
        summary="A regional administration announced additional citizen services.",
        content="The municipal government said the program will reach more residents.",
        url="https://example.test/cross-language/foreign-governance",
        category="国际政治",
        published_at=utc_naive_now() - timedelta(minutes=1),
    )

    candidates = select_candidates(db, subscription, demo_mode=False, limit=10)

    assert [article.id for article in candidates] == [matching.id]
    assert unrelated.id not in {article.id for article in candidates}


def test_chinese_employment_preference_matches_english_business_news(db):
    user = User(email="employment-preference@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(
        user_id=user.id,
        topics=["财经"],
        keywords=["就业"],
    )
    db.add(subscription)
    db.flush()

    matching = add_article(
        db,
        source_id="cross-language-feed",
        title="New workforce program expands employment opportunities",
        summary="The national employment program adds vocational training.",
        content="Employers and workers can apply for the employment support program.",
        url="https://example.test/cross-language/employment-match",
        category="international finance",
        published_at=utc_naive_now(),
    )
    unrelated = add_article(
        db,
        source_id="cross-language-feed",
        title="Business leaders discuss changes to international markets",
        summary="The conference focused on trade and financial policy.",
        content="The conference focused on trade and market regulation.",
        url="https://example.test/cross-language/employment-no-match",
        category="international finance",
        published_at=utc_naive_now() - timedelta(minutes=1),
    )

    candidates = select_candidates(db, subscription, demo_mode=False, limit=10)

    assert [article.id for article in candidates] == [matching.id]
    assert unrelated.id not in {article.id for article in candidates}


def test_english_topic_aliases_require_word_boundaries(db):
    user = User(email="english-topic-boundaries@example.com")
    db.add(user)
    db.flush()
    ai_subscription = Subscription(
        user_id=user.id,
        topics=["人工智能"],
        keywords=["人工智能"],
    )
    db.add(ai_subscription)
    db.flush()

    false_ai_match = add_article(
        db,
        source_id="cross-language-feed",
        title="Results remain stable after the review",
        summary="The article describes an ordinary review process.",
        content="The results remain unchanged.",
        url="https://example.test/cross-language/false-ai-match",
        category="international",
        published_at=utc_naive_now(),
    )
    true_ai_match = add_article(
        db,
        source_id="cross-language-feed",
        title="New AI system improves language translation",
        summary="Researchers describe an artificial intelligence system.",
        content="The AI system uses machine learning.",
        url="https://example.test/cross-language/true-ai-match",
        category="international technology",
        published_at=utc_naive_now() - timedelta(minutes=1),
    )

    assert select_candidates(db, ai_subscription, demo_mode=False, limit=10) == [true_ai_match]

    arts_user = User(email="english-arts-boundaries@example.com")
    db.add(arts_user)
    db.flush()
    arts_subscription = Subscription(
        user_id=arts_user.id,
        topics=["文化"],
        keywords=["艺术"],
    )
    db.add(arts_subscription)
    db.flush()
    false_art_match = add_article(
        db,
        source_id="cross-language-feed",
        title="An article reviews a new global service",
        summary="The article describes a recently launched service.",
        content="The article includes general background information.",
        url="https://example.test/cross-language/false-art-match",
        category="国际文化",
        published_at=utc_naive_now(),
    )
    true_art_match = add_article(
        db,
        source_id="cross-language-feed",
        title="Art exhibition opens in London",
        summary="The exhibition features works by emerging artists.",
        content="The art exhibition presents sculpture and painting.",
        url="https://example.test/cross-language/true-art-match",
        category="国际文化",
        published_at=utc_naive_now() - timedelta(minutes=1),
    )

    assert select_candidates(db, arts_subscription, demo_mode=False, limit=10) == [true_art_match]
    assert false_ai_match.id != true_ai_match.id
    assert false_art_match.id != true_art_match.id


def test_topic_alias_in_body_needs_repeated_evidence_when_not_in_title_summary_or_category(db):
    user = User(email="topic-body-evidence@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(user_id=user.id, topics=["社会"], keywords=[])
    db.add(subscription)
    db.flush()
    now = utc_naive_now()

    incidental = add_article(
        db,
        source_id="cross-language-feed",
        title="Wall Street Retreats from AI Sector Amid Concerns Over Cost",
        summary="The demand for AI compute is flat and sensitive to price changes.",
        content="The market outlook was described as social in one passing sentence.",
        url="https://example.test/topic-body/incidental",
        category="国际财经",
        published_at=now,
    )
    sustained = add_article(
        db,
        source_id="cross-language-feed",
        title="A new report reviews public services",
        summary="The report describes changing local services.",
        content="Community groups respond to the change. Society-wide effects remain under discussion.",
        url="https://example.test/topic-body/sustained",
        category="国际综合",
        published_at=now - timedelta(minutes=1),
    )

    candidates = select_candidates(db, subscription, demo_mode=False, limit=10)

    assert [article.id for article in candidates] == [sustained.id]
    assert incidental.id not in {article.id for article in candidates}


def test_society_topic_does_not_match_social_media_descriptor_in_finance_article(db):
    user = User(email="social-media-finance@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(user_id=user.id, topics=["社会"], keywords=[])
    db.add(subscription)
    db.flush()
    article = add_article(
        db,
        source_id="cross-language-feed",
        title="Wall Street Retreats from AI Sector Amid Concerns Over Cost",
        summary="AI firms struggle to justify valuations and secure funding.",
        content=("The social media giant reported higher debt costs. " * 3),
        url="https://example.test/topic-body/social-media-descriptor",
        category="国际财经",
        published_at=utc_naive_now(),
    )

    assert select_candidates(db, subscription, demo_mode=False, limit=10) == []
    assert article.category == "国际财经"


def test_international_preference_does_not_match_domestic_item_from_mixed_news_feed(db):
    user = User(email="international-preference@example.com")
    db.add(user)
    db.flush()
    subscription = Subscription(user_id=user.id, topics=["国际"], keywords=[])
    db.add(subscription)
    db.flush()

    now = utc_naive_now()
    domestic = add_article(
        db,
        source_id="easy-mandarin-news",
        title="中国年轻人关注养老新职业",
        summary="An article about elderly care work in China.",
        content="As Chinese society ages, more young people enter the elderly care industry.",
        url="https://easymandarin.news/en/articles/domestic-example",
        category="综合新闻（AI 二次整理）",
        published_at=now,
    )
    international = add_article(
        db,
        source_id="easy-mandarin-news",
        title="美中俄会谈能让乌克兰战争结束吗？",
        summary="Talks among the United States, Russia and Ukraine.",
        content="The United States and Russia discuss the war in Ukraine and European security.",
        url="https://easymandarin.news/en/articles/international-example",
        category="综合新闻（AI 二次整理）",
        published_at=now - timedelta(minutes=1),
    )

    candidates = select_candidates(db, subscription, demo_mode=False, limit=10)

    assert [article.id for article in candidates] == [international.id]
    assert domestic.id not in {article.id for article in candidates}
