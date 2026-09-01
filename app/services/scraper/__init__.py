"""Resilient IPO scraping.

Stages are separated so each can be tested and replaced independently::

    client.py     HTTP fetching with retries and connection reuse
    extractor.py  locate the dataset and identify its columns
    field_map.py  semantic field definitions (the resilience layer)
    models.py     value objects passed between stages
    normalizer.py raw cells -> typed canonical values
    validator.py  per-record checks and run confidence scoring
    pipeline.py   orchestration and the persist/abort decision

This package deliberately re-exports nothing.  ``pipeline`` depends on the
repository layer, while the repository layer depends on ``models`` for its
DTOs; eagerly importing the pipeline here would close that loop into a circular
import.  Import the submodule you need directly::

    from app.services.scraper.pipeline import ScrapePipeline
    from app.services.scraper.models import NormalizedIPO
"""
