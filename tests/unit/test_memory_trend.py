"""The soak's memory verdict (tests/load/memory_trend.py): warm-up excluded, gateways judged."""

from tests.load.memory_trend import WARM_UP_S, parse, verdict

START = 1_800_000_000


def _samples(name: str, mib: list[float], every_s: int = 300) -> list[str]:
    return [f"{START + i * every_s} {name} {m}MiB / 15.61GiB" for i, m in enumerate(mib)]


def test_parse_reads_docker_stats_units() -> None:
    series = parse([f"{START} finops-db-1 1.5GiB / 15.61GiB", f"{START} web 900KiB / 15.61GiB"])
    assert series == {"finops-db-1": [(START, 1.5 * 1024**3)], "web": [(START, 900 * 1024.0)]}


def test_growth_is_measured_from_the_end_of_warm_up() -> None:
    # 150 MiB at the start, 300 after warm-up, 320 at the end: the warm-up is not a leak.
    rows, leaking = verdict(parse(_samples("finops-gateway-1", [150, 250, 290, 300, 310, 320])))
    assert not leaking
    assert rows[2] == "| finops-gateway-1 | 300 MiB | 320 MiB | 1.07x |"
    assert WARM_UP_S == 900  # the fourth sample, five minutes apart


def test_a_gateway_that_keeps_growing_is_a_leak() -> None:
    rows, leaking = verdict(parse(_samples("finops-gateway-2", [150, 150, 150, 150, 260, 400])))
    assert leaking
    assert rows[2].endswith("(leak) |")


def test_only_gateway_replicas_are_judged() -> None:
    _, leaking = verdict(parse(_samples("finops-db-1", [200, 200, 200, 200, 600, 900])))
    assert not leaking  # PostgreSQL fills its own caches by design
