import pathlib
import re

import polars as pl

DIGITS = re.compile(r"[0-9.]+")

FACTORS = {
    "": 1,
    "s": 1,
    "B": 1,
    "kB": 1024,
    "MB": 1024 * 1024,
    "GB": 1024 * 1024 * 1024,
}


def parse_memory(s):
    m = DIGITS.match(s)
    if not m:
        return 0

    return int(float(m.group()) * FACTORS[s[m.end() :]])


def parse_counters(s):
    components = s.lstrip("[").rstrip("]").strip().split(";")
    components = [
        dict(zip(["counter", "value"], (s.strip() for s in component.split("="))))
        for component in components
    ]
    return components


def read_profile_csv(profile_path):
    df = pl.read_csv(profile_path)
    df = df.with_columns(pl.col("time").str.strip_suffix("s").cast(pl.Float64))
    fname = df.get_column("pass name")[0].removeprefix("file=").rstrip("/")
    df = df.with_columns(
        (
            "/"
            + pl.col("pass name")
            .str.strip_prefix(f"file={fname}")
            .str.strip_chars_start("/")
        )
        .str.strip_chars_end("/")
        .alias("pass name")
    )
    df = df.with_columns(
        pl.col("alloc").map_elements(parse_memory, return_dtype=pl.Int64),
        pl.col("top-heap").map_elements(parse_memory, return_dtype=pl.Int64),
        pl.col("absolute-top-heap").map_elements(parse_memory, return_dtype=pl.Int64),
    )
    df = df.with_columns(
        pl.col("counters").map_elements(
            parse_counters,
            return_dtype=pl.List(pl.Struct({"counter": pl.String, "value": pl.String})),
        )
    )
    df = df.insert_column(0, pl.lit(fname).alias("file"))
    df = df.rename({"top-heap": "top heap", "absolute-top-heap": "absolute top heap"})
    return df


def hierarchize(df):
    data = {}
    for pass_name, time in (
        df.group_by("pass name")
        .agg(pl.col("time").sum())
        .sort("pass name")
        .with_columns(pl.col("pass name").str.split("/").list.slice(1))
        .iter_rows()
    ):
        current = data
        for component in pass_name:
            current = current.setdefault(component, {})
        current[""] = time
    return data


def print_timings(timings, name="", *, depth=0, prefix="", total=None, max_depth=None):
    if max_depth is not None and depth > max_depth:
        return

    if total is None:
        extra_prefix = ""
        total = timings.get("", 0.0)
    else:
        extra_prefix = "  | "
    time = timings.get("", 0.0)
    percent = (time / total) * 100
    print(
        f"{prefix}{extra_prefix.replace('|', '+')}{time:>6.02f}s {percent:>5.01f}% {name}"
    )
    for k, v in sorted(
        ((k, v) for k, v in timings.items() if k), key=lambda x: -x[1].get("", 0.0)
    ):
        print_timings(
            v,
            k,
            depth=depth + 1,
            prefix=prefix + extra_prefix,
            total=total,
            max_depth=max_depth,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=pathlib.Path)
    parser.add_argument("--cache", type=pathlib.Path)

    ns = parser.parse_args()

    if ns.cache is not None and ns.cache.exists():
        df = pl.read_parquet(ns.cache)
    else:
        df = pl.concat(read_profile_csv(p) for p in ns.path.glob("**/*.csv"))

        if ns.cache is not None:
            df.write_parquet(ns.cache)

    print_timings(hierarchize(df))
