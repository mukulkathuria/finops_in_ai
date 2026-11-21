"""Memory over a soak (.github/workflows/soak.yml): the samples `docker stats` took every few
minutes, as a table, and a verdict. A gateway replica whose memory more than doubles between the
end of warm-up and the end of the run fails it: that is a leak, not caches and pools filling up.

  python3 tests/load/memory_trend.py /tmp/memory.txt
  (lines: <epoch seconds> <container> <used> / <limit>, as `docker stats` prints MemUsage)

Runs on the runner's own Python, so it keeps to the standard library.
"""

import re
import sys
from collections import defaultdict
from collections.abc import Iterable

UNITS = {
    "B": 1,
    "kB": 1000,
    "KiB": 1024,
    "MB": 1000**2,
    "MiB": 1024**2,
    "GB": 1000**3,
    "GiB": 1024**3,
}
WARM_UP_S = 15 * 60  # caches and connection pools fill in the first minutes
MAX_GROWTH = 2.0
_LINE = re.compile(r"^(\d+) (\S+) ([\d.]+)([kKMG]?i?B) /")

Series = dict[str, list[tuple[int, float]]]


def parse(lines: Iterable[str]) -> Series:
    """Memory samples per container, in bytes, keyed by the epoch second they were taken."""
    series: defaultdict[str, list[tuple[int, float]]] = defaultdict(list)
    for line in lines:
        m = _LINE.match(line.strip())
        if m and m[4] in UNITS:
            series[m[2]].append((int(m[1]), float(m[3]) * UNITS[m[4]]))
    return dict(series)


def verdict(series: Series) -> tuple[list[str], bool]:
    """A Markdown table of each container's memory, and whether a gateway replica leaks."""
    rows = ["| Container | After warm-up | At the end | Growth |", "| --- | --- | --- | --- |"]
    leaking = False
    for name in sorted(series):
        samples = sorted(series[name])
        start = samples[0][0]
        warm = next((used for at, used in samples if at - start >= WARM_UP_S), samples[-1][1])
        end = samples[-1][1]
        growth = end / warm if warm else 1.0
        flag = ""
        if "gateway" in name and growth > MAX_GROWTH:
            leaking, flag = True, " (leak)"
        rows.append(
            f"| {name} | {warm / 2**20:.0f} MiB | {end / 2**20:.0f} MiB | {growth:.2f}x{flag} |"
        )
    return rows, leaking


def main(path: str) -> int:
    with open(path, encoding="utf-8") as samples:
        series = parse(samples)
    if not series:
        sys.stderr.write("no memory samples were taken\n")
        return 1
    rows, leaking = verdict(series)
    sys.stdout.write("### Memory over the soak\n\n" + "\n".join(rows) + "\n")
    return 1 if leaking else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
