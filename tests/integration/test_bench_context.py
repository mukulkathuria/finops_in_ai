"""The Phase 4 latency benchmark runs end to end at a tiny scale on every push (the full-scale run
is the `bench` workflow): it seeds, rebuilds the chunk indexes, measures each query through the
gateway's own code as the `app` role and writes the report."""

import pytest

from tests.load.bench_context import TARGETS, Scale, run


@pytest.mark.usefixtures("test_db")  # skipped, like every integration test, without a server
async def test_the_latency_benchmark_runs_end_to_end() -> None:
    code, report = await run(Scale(orgs=2, chunks=400, edges=800, projects=2, samples=20))
    for query in TARGETS:
        assert f"| {query} |" in report
    assert "EXPLAIN vector search" in report
    assert "Index Scan" in report or "Bitmap" in report or "Seq Scan" in report
    assert code in (0, 1)  # timing on a shared runner at this scale gates nothing
