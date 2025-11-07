import hashlib
from pathlib import Path

from pipelines.day1 import confidence_scoring
from tests.utils import create_canonical_bundle


def test_confidence_scoring_is_deterministic(tmp_path: Path):
    bundle_root = create_canonical_bundle(
        tmp_path,
        youcom=[
            {
                "company": "Acme AI",
                "press_articles": ["https://you.com/news/acme"],
                "news_sources": ["TechCrunch"],
                "youcom_verified": True,
            }
        ],
        tavily=[
            {
                "company": "Acme AI",
                "proof_links": ["https://tavily.com/posts/acme"],
                "tavily_verified": True,
            }
        ],
        exa=[{"company": "Acme AI", "source_url": "https://exa.ai/records/acme"}],
    )

    output_path = tmp_path / "day1_output.json"

    confidence_scoring.run_pipeline(bundle_root, output_path)
    first_bytes = output_path.read_bytes()
    confidence_scoring.run_pipeline(bundle_root, output_path)
    second_bytes = output_path.read_bytes()

    assert first_bytes == second_bytes
    assert hashlib.sha256(first_bytes).hexdigest() == hashlib.sha256(second_bytes).hexdigest()
