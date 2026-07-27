import difflib
import enum
import hashlib
import json
import logging
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import cached_property
from typing import ClassVar

import polars as pl
from patiencediff import PatienceSequenceMatcher

CACHE_HOME = pathlib.Path(os.getenv("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache")
CACHE_DIR = CACHE_HOME.joinpath("ox")

DEFAULT_REVISION = "5.4.0-ox2"

PACKAGE_DATA_ROOT = os.path.dirname(__file__)

logger = logging.getLogger(__name__)


@dataclass
class OxcamlRevision:
    user: str
    repo: str
    rev: str

    @classmethod
    def parse(cls, value):
        index = value.rfind("@")
        if index < 0:
            user = "oxcaml"
            repo = "oxcaml"
            rev = value
        else:
            rev, value = value[:index], value[index + 1 :]

            index = value.find("/")
            if index < 0:
                user = value
                repo = "oxcaml"
            else:
                user, repo = value[:index], value[index + 1 :]

        return cls(user, repo, rev)

    def __str__(self):
        s = "" if self.repo == "oxcaml" else f"/{self.repo}"
        s = "" if not s and self.user == "oxcaml" else f"@{self.user}{s}"
        return self.rev + s

    @cached_property
    def url(self):
        return f"https://github.com/{self.user}/{self.repo}/archive/{self.rev}.tar.gz"

    @cached_property
    def sha256(self):
        return self.prefetch_url[0]

    @cached_property
    def prefetch_url(self):
        out = subprocess.run(
            ["nix-prefetch-url", "--print-path", self.url],
            capture_output=True,
            text=True,
            check=True,
        )
        base32_hash, nix_path = out.stdout.strip().split()
        return (base32_hash, nix_path)

    def unpack(self, path):
        path = pathlib.Path(path)

        _, nix_path = self.prefetch_url
        with tarfile.open(nix_path) as tar:
            tar.extractall(path, filter="data")

        children = list(path.iterdir())
        if len(children) == 1:
            return children[0]

        return path


@dataclass
class OxcamlCompiler:
    revision: OxcamlRevision

    def __str__(self):
        return str(self.revision)

    @property
    def sha256(self):
        return self.revision.sha256

    def _run(self, *args, **kwargs):
        args = (
            "nix-shell",
            "--argstr",
            "url",
            self.revision.url,
            "--argstr",
            "hash",
            f"sha256:{self.revision.sha256}",
            "--argstr",
            "version",
            self.revision.rev,
            os.path.join(PACKAGE_DATA_ROOT, "oxcaml-shell.nix"),
            *args,
        )
        return subprocess.run(args, **kwargs)  # noqa: PLW1510

    def shell(self):
        self._run(check=True)

    def run(self, *args):
        self._run("--run", shlex.join(args), check=True)


class BenchmarkMode(enum.Enum):
    PROFILE = 0
    FEXPR = 1


@dataclass
class OxcamlConfiguration:
    params: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    columns: int = 180

    def tojson(self):
        return json.dumps(
            {"params": self.params, "flags": self.flags, "columns": self.columns},
            sort_keys=True,
            separators=(",", ":"),
        )

    def __str__(self):
        if self.params:
            s = (
                "OCAMLPARAM=_,"
                + ",".join(f"{key}={value}" for key, value in self.params.items())
                + " "
            )
        else:
            s = ""

        if self.flags:
            s = (s + " " if s else "") + " ".join(self.flags)

        return s or "(default)"

    @cached_property
    def sha256(self):
        return hashlib.sha256(self.tojson().encode()).hexdigest()

    def write_boot_ws(self, fp, *, dump_fexpr, profile_dir):
        ocamlopt_flags = self.flags
        if dump_fexpr:
            ocamlopt_flags += ["-dfexpr-annot", "-dcanonical-ids", "-color", "never"]

        ocamlparams = [f"{key}={value}" for key, value in self.params.items()]
        if profile_dir:
            ocamlparams += [f"dump-dir={profile_dir},dump-into-csv=1,profile=1"]

        fp.write(f"""(lang dune 2.8)
(context (default
    (name default)
    (profile main)
    (env (_
        (flags (:standard -warn-error +A -alert -unsafe_multidomain -alert -unsafe_effects))
        (ocamlopt_flags ({" ".join([":standard"] + ocamlopt_flags)}))
        (env-vars
            ("OCAMLPARAM" "{",".join(["_"] + ocamlparams)}")
            ("COLUMNS" "{self.columns}"))))))
""")

    def write_build_sh(self, fp, *, use_colley=None, build_dir, jobs):
        if use_colley is None:
            use_colley = bool(shutil.which("colley-run"))

        make_cmd = "make boot-compiler"
        if use_colley:
            make_cmd = f"colley-run {jobs} -- {make_cmd}"

        fp.write(f"""#!/usr/bin/env bash
set -euo pipefail

cd {shlex.quote(os.fspath(build_dir))}

autoconf --force
./configure --enable-runtime5 --disable-optional-checks

export DUNE_JOBS={jobs}
{make_cmd}
""")


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


@dataclass
class OxcamlBenchmark:
    compiler: OxcamlCompiler
    revision: OxcamlRevision
    configuration: OxcamlConfiguration

    def __str__(self):
        return "\n".join(
            [
                f"Benchmark: oxcaml {self.revision}",
                f"Compiler: {self.compiler}",
                f"Configuration: {self.configuration}",
            ]
        )

    @property
    def base_path(self):
        return CACHE_DIR.joinpath(
            self.compiler.sha256, self.revision.sha256, self.configuration.sha256
        )

    @property
    def profile_path(self):
        return self.base_path / "profile.parquet"

    @property
    def fexpr_path(self):
        return self.base_path / "fexpr.tar.gz"

    def _run(self, *, mode: BenchmarkMode, jobs=None):
        if not jobs or mode is BenchmarkMode.PROFILE:
            jobs = 1

        profile_dir = None
        with tempfile.TemporaryDirectory() as unpack_dir:
            if mode is BenchmarkMode.PROFILE:
                profile_dir = os.path.join(unpack_dir, "profile")

            build_dir = self.revision.unpack(os.path.join(unpack_dir, "source"))

            boot_ws = os.path.join(build_dir, "duneconf", "boot.ws")
            with open(boot_ws, "w") as fp:
                self.configuration.write_boot_ws(
                    fp, profile_dir=profile_dir, dump_fexpr=mode is BenchmarkMode.FEXPR
                )

            build_sh = os.path.join(unpack_dir, "build.sh")
            with open(build_sh, "w") as fp:
                self.configuration.write_build_sh(fp, build_dir=build_dir, jobs=jobs)

            self.compiler.run("bash", build_sh)

            if profile_dir is not None:
                self.profile_path.parent.mkdir(parents=True, exist_ok=True)
                pl.concat(
                    read_profile_csv(p)
                    for p in list(pathlib.Path(profile_dir).glob("**/*.csv"))
                ).write_parquet(self.profile_path)

            if mode is BenchmarkMode.FEXPR:
                self.fexpr_path.parent.mkdir(parents=True, exist_ok=True)
                with tarfile.open(self.fexpr_path, "x:gz") as tar:
                    dune_dir = build_dir.joinpath("_build", "default")
                    for path in dune_dir.glob("**/*.simplify.fl"):
                        tar.add(path, "simplify" / path.relative_to(dune_dir))

                logger.info(f"Stored fexpr output in {self.fexpr_path}")

    def record_profile(self):
        logger.info("Recording profile for:")
        for line in str(self).splitlines():
            logger.info("    " + line)

        if self.profile_path.exists():
            logger.info(f"Using cached results from: {self.profile_path}")
        else:
            self._run(mode=BenchmarkMode.PROFILE)

        return self.profile_path

    def record_fexpr(self, **kwargs):
        logger.info("Dumping fexpr for:")
        for line in str(self).splitlines():
            logger.info("    " + line)

        if self.fexpr_path.exists():
            logger.info(f"Using cached results from: {self.fexpr_path}")
        else:
            self._run(mode=BenchmarkMode.FEXPR, **kwargs)

        return self.fexpr_path


def hierarchize_list(df_list):
    df_time = pl.concat(
        [
            df.group_by("pass name").agg(pl.col("time").sum().alias(f"time_{i}"))
            for i, df in enumerate(df_list)
        ],
        how="align_left",
    ).select(
        pl.col("pass name"),
        pl.concat_list([f"time_{i}" for i in range(len(df_list))]).alias("time"),
    )

    data = {}
    for pass_name, time in df_time.with_columns(
        pl.col("pass name").str.split("/").list.slice(1)
    ).iter_rows():
        current = data
        for component in pass_name:
            current = current.setdefault(component, {})
        current[""] = time
    return data


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
        current[""] = [time]
    return data


def print_timings(timings, name="", *, depth=0, prefix="", total=None, max_depth=None):
    if max_depth is not None and depth > max_depth:
        return

    twidth = 6
    pwidth = 5

    if total is None:
        extra_prefix = ""
        total = timings.get("", [0.0])[0]
    else:
        extra_prefix = "  | "

    line = ""
    first = True
    reference = None
    for time in timings.get("", []):
        if not time:
            line += f"{'----':>{twidth}}  {'':>{pwidth}}  "
        else:
            if reference is None:
                palign = ">"
                percent = (time / total) * 100

                if first:
                    reference = time
            else:
                palign = "+"
                percent = (time - reference) / total * 100
            line += f"{time:>{twidth}.02f}s {percent:{palign}{pwidth}.01f}% "

        first = False
    print(f"{prefix}{extra_prefix.replace('|', '+')}{line}{name}")
    for k, v in sorted(
        ((k, v) for k, v in timings.items() if k), key=lambda x: -x[1].get("", [0.0])[0]
    ):
        print_timings(
            v,
            k,
            depth=depth + 1,
            prefix=prefix + extra_prefix,
            total=total,
            max_depth=max_depth,
        )


class IgnoredSymbol:
    def __init__(self, name):
        self._name = name

    def __str__(self):
        return self._name

    def __hash__(self):
        return hash("$")

    def __eq__(self, other):
        if isinstance(other, IgnoredSymbol):
            return True

        return NotImplemented


INVALID_RE = re.compile(r'\binvalid\s+(?:"[^"\n]*")')
WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]+")
NUM_RE = re.compile(r"[0-9]+")
SYMBOL_RE = re.compile(r"\$([^\s\),`]+\b|`[^\s`]+`)")


def normalize_line(s):
    s = INVALID_RE.sub("invalid", s)
    return SYMBOL_RE.sub("$", s)


def chunkiter(r, s):
    pos = 0
    for m in r.finditer(s):
        yield (s[pos : m.start()], m.group())
        pos = m.end()
    yield (s[pos:], None)


def fexpr_line(s):
    # Pretend invalid payload doesn't exist
    s = INVALID_RE.sub("invalid", s)

    chunks = []
    for before_symbol, symbol in chunkiter(SYMBOL_RE, s):
        for before_word, word in chunkiter(WORD_RE, before_symbol):
            for before_number, number in chunkiter(NUM_RE, before_word):
                chunks.extend(before_number)

                if number is not None:
                    chunks.append(number)

            if word is not None:
                chunks.append(word)

        if symbol is not None:
            chunks.append(IgnoredSymbol(symbol))

    return tuple(chunks)


class FexprDiffer:
    # Adapted from the stdlib's difflib.Differ

    PREFIXES: ClassVar[dict[str, str]] = {
        "+": "\033[30;42m+|\033[0m\033[32m",
        "-": "\033[30;41m-|\033[0m\033[31m",
        "=": "\033[30;100m |\033[0m",
    }
    prefix_location = "\033[30;100m@|\033[0m\033[1m"

    style_reset = "\033[0m"

    def compare(self, a, b):

        a_norm = [normalize_line(line) for line in a]
        b_norm = [normalize_line(line) for line in b]

        crunch = PatienceSequenceMatcher(None, a=a_norm, b=b_norm)
        for group in crunch.get_grouped_opcodes(3):
            # Note: line numbers start at 1, not 0
            # Note: display lineno instead of lineno,1
            _, a_start, _, b_start, _ = group[0]
            _, _, a_end, _, b_end = group[-1]
            a_len = a_end - a_start
            a_len = f",{a_len}" if a_len != 1 else ""
            b_len = b_end - b_start
            b_len = f",{b_len}" if b_len != 1 else ""
            yield (
                self.prefix_location
                + f"-{a_start + 1}{a_len} +{b_start + 1}{b_len}"
                + self.style_reset
                + " ============================================================\n"
            )

            for tag, i1, i2, j1, j2 in group:
                if tag == "equal":
                    yield from self._dump("=", a, i1, i2)

                elif tag == "delete":
                    yield from self._dump("-", a, i1, i2)

                elif tag == "insert":
                    yield from self._dump("+", b, j1, j2)

                elif tag == "replace":
                    yield from self._fancy_replace(a, i1, i2, b, j1, j2)

                else:
                    raise ValueError(f"unknown tag {tag!r}")

    def _plain_replace(self, a, alo, ahi, b, blo, bhi):
        assert alo < ahi and blo < bhi
        # dump the shorter block first -- reduces the burden on short-term
        # memory if the blocks are of very different sizes
        if bhi - blo < ahi - alo:
            first = self._dump("+", b, blo, bhi)
            second = self._dump("-", a, alo, ahi)
        else:
            first = self._dump("-", a, alo, ahi)
            second = self._dump("+", b, blo, bhi)

        for g in first, second:
            yield from g

    def _dump(self, prefix, x, lo, hi):
        prefix = self.PREFIXES.get(prefix, prefix)
        for i in range(lo, hi):
            yield prefix + x[i] + "\033[0m"

    def _fancy_replace(self, a, alo, ahi, b, blo, bhi):
        r"""
        When replacing one block of lines with another, search the blocks
        for *similar* lines; the best-matching pair (if any) is used as a
        synch point, and intraline difference marking is done on the
        similar pair. Lots of work, but often worth it.

        Example:

        >>> d = Differ()
        >>> results = d._fancy_replace(['abcDefghiJkl\n'], 0, 1,
        ...                            ['abcdefGhijkl\n'], 0, 1)
        >>> print(''.join(results), end="")
        - abcDefghiJkl
        ?    ^  ^  ^
        + abcdefGhijkl
        ?    ^  ^  ^
        """
        # Don't synch up unless the lines have a similarity score above
        # cutoff. Previously only the smallest pair was handled here,
        # and if there are many pairs with the best ratio, recursion
        # could grow very deep, and runtime cubic. See:
        # https://github.com/python/cpython/issues/119105
        #
        # Later, more pathological cases prompted removing recursion
        # entirely.
        cutoff = 0.74999
        cruncher = difflib.SequenceMatcher(
            lambda c: isinstance(c, str) and difflib.IS_CHARACTER_JUNK(c)
        )
        crqr = cruncher.real_quick_ratio
        cqr = cruncher.quick_ratio
        cr = cruncher.ratio

        WINDOW = 10
        agroup = []
        bgroup = []
        best_i = best_j = None
        dump_i, dump_j = alo, blo  # smallest indices not yet resolved
        for j in range(blo, bhi):
            cruncher.set_seq2(fexpr_line(b[j]))
            # Search the corresponding i's within WINDOW for rhe highest
            # ratio greater than `cutoff`.
            aequiv = alo + (j - blo)
            arange = range(max(aequiv - WINDOW, dump_i), min(aequiv + WINDOW + 1, ahi))
            if not arange:  # likely exit if `a` is shorter than `b`
                break
            best_ratio = cutoff
            for i in arange:
                cruncher.set_seq1(fexpr_line(a[i]))
                # Ordering by cheapest to most expensive ratio is very
                # valuable, most often getting out early.
                if crqr() > best_ratio and cqr() > best_ratio and cr() > best_ratio:
                    best_i, best_j, best_ratio = i, j, cr()

            if best_i is None:
                # found nothing to synch on yet - move to next j
                continue

            # pump out straight replace from before this synch pair
            yield from self._fancy_helper(
                a, dump_i, best_i, b, dump_j, best_j, agroup, bgroup
            )
            # do intraline marking on the synch pair
            aelt, belt = a[best_i], b[best_j]
            if aelt != belt:
                aelt = fexpr_line(aelt)
                belt = fexpr_line(belt)
                atags = btags = ""
                cruncher.set_seqs(aelt, belt)
                for tag, ai1, ai2, bj1, bj2 in cruncher.get_opcodes():
                    sa = "".join(map(str, aelt[ai1:ai2]))
                    sb = "".join(map(str, belt[bj1:bj2]))
                    if tag == "replace":
                        atags += "\033[31m" + sa + "\033[0m"
                        btags += "\033[32m" + sb + "\033[0m"
                    elif tag == "delete":
                        atags += "\033[31m" + sa + "\033[0m"
                    elif tag == "insert":
                        btags += "\033[32m" + sb + "\033[0m"
                    elif tag == "equal":
                        atags += "\033[2m" + sa + "\033[0m"
                        btags += sb
                    else:
                        raise ValueError(f"unknown tag {tag!r}")
                agroup.append(self.PREFIXES["-"] + self.style_reset + atags)
                bgroup.append(self.PREFIXES["+"] + self.style_reset + btags)
            else:
                # the synch pair is identical
                yield from self._fancy_group(agroup, bgroup)
                yield self.PREFIXES["="] + aelt + self.style_reset
            dump_i, dump_j = best_i + 1, best_j + 1
            best_i = best_j = None

        # pump out straight replace from after the last synch pair
        yield from self._fancy_group(agroup, bgroup)
        yield from self._fancy_helper(a, dump_i, ahi, b, dump_j, bhi, agroup, bgroup)

    def _fancy_group(self, agroup, bgroup):
        yield from agroup
        yield from bgroup
        agroup.clear()
        bgroup.clear()

    def _fancy_helper(self, a, alo, ahi, b, blo, bhi, agroup, bgroup):
        if alo < ahi:
            yield from self._fancy_group(agroup, bgroup)
            if blo < bhi:
                yield from self._plain_replace(a, alo, ahi, b, blo, bhi)
            else:
                yield from self._dump("-", a, alo, ahi)
        elif blo < bhi:
            yield from self._fancy_group(agroup, bgroup)
            yield from self._dump("+", b, blo, bhi)


def compare_diffs(prev_str, nexts):
    differ = FexprDiffer()
    prev_lines = prev_str.splitlines(keepends=True)

    for next_str in nexts:
        next_lines = next_str.splitlines(keepends=True)
        yield from differ.compare(prev_lines, next_lines)


def compare_tars(prev_tar: tarfile.TarFile, tars: list[tarfile.TarFile]):
    try:
        for prev_info in prev_tar.getmembers():
            name = prev_info.name
            if not name.endswith(".fl"):
                continue

            prev = prev_tar.extractfile(name).read().decode("utf-8")
            nexts = [tar.extractfile(name).read().decode("utf-8") for tar in tars]

            diff_lines = list(compare_diffs(prev, nexts))
            if diff_lines:
                _, name = name.split(os.sep, 1)
                sys.stdout.write(f"\033[31m------\033[0m \033[1ma/{name}\033[0m\n")
                sys.stdout.write(f"\033[32m++++++\033[0m \033[1mb/{name}\033[0m\n")
                sys.stdout.writelines(diff_lines)
    except BrokenPipeError:
        pass


from copy import copy
from optparse import Option, OptionParser, make_option


def check_param(_option, _opt, value):
    if "=" not in value:
        value = f"{value}=1"

    return value.split("=", 1)


def check_revision(option, opt, value):
    return OxcamlRevision.parse(value)


class OxOption(Option):
    TYPES = Option.TYPES + ("revision", "param")
    TYPE_CHECKER = copy(Option.TYPE_CHECKER)
    TYPE_CHECKER["revision"] = check_revision
    TYPE_CHECKER["param"] = check_param


oxcaml_benchmark_options = [
    OxOption("-P", action="append", dest="params", type="param"),
    make_option("-F", action="append", dest="flags"),
]


def parse_benchmarks(prog, args):
    parser = OptionParser(usage=f"%prog {prog}", option_list=oxcaml_benchmark_options)
    parser.disable_interspersed_args()

    while args:
        compiler = OxcamlCompiler(OxcamlRevision.parse(args[0]))
        opts, args = parser.parse_args(args[1:])
        config = OxcamlConfiguration(dict(opts.params or {}), opts.flags or [])
        yield compiler, config


def main():
    logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

    parser = OptionParser(
        description="An OxCaml revision manager",
        usage="%prog [options] command ...",
        option_class=OxOption,
    )
    parser.disable_interspersed_args()

    parser.add_option(
        "-r",
        "--revision",
        dest="rev",
        default=os.getenv("OXCAML_REVISION") or DEFAULT_REVISION,
        type="revision",
    )

    opts, args = parser.parse_args()

    if not args:
        parser.error("missing argument: command")

    command, *args = args

    rev: OxcamlRevision = opts.rev
    match command:
        case "ocamlopt" | "opt":
            OxcamlCompiler(rev).run("ocamlopt", *args)

        case "shell":
            if args:
                parser.error("unexpected arguments for shell command")

            OxcamlCompiler(rev).shell()

        case "profile":
            profile_parser = OptionParser(usage="%prog profile [options] ...")
            profile_parser.disable_interspersed_args()
            profile_opts, args = profile_parser.parse_args(args)

            if not args:
                parser.error("missing argument for profile: record")

            profile_cmd, *args = args
            match profile_cmd:
                case "record" | "report":
                    record_parser = OptionParser(
                        usage="%prog profile record [options] ..."
                    )
                    record_parser.disable_interspersed_args()
                    _record_opts, args = record_parser.parse_args(args)

                    df_list = []
                    for compiler, config in parse_benchmarks("profile record", args):
                        benchmark = OxcamlBenchmark(compiler, rev, config)
                        profile_path = benchmark.record_profile()

                        if profile_cmd != "record":
                            df_list.append(pl.read_parquet(profile_path))

                    if df_list:
                        print_timings(hierarchize_list(df_list))

                case _:
                    parser.error(f"unknown profile subcommand: {profile_cmd}")

        case "fexpr":
            fexpr_parser = OptionParser(usage="%prog fexpr [options] ...")
            fexpr_parser.disable_interspersed_args()
            fexpr_parser.add_option("-j", "--jobs", type=int, dest="jobs")
            fexpr_opts, args = fexpr_parser.parse_args(args)

            if not args:
                parser.error("missing fexpr subcommand")

            fexpr_command, *args = args

            match fexpr_command:
                case "dump" | "diff":
                    benchmarks = list(parse_benchmarks(f"fexpr {fexpr_command}", args))

                    expected_len = 1 if fexpr_command == "dump" else 2
                    if len(benchmarks) != expected_len:
                        fexpr_parser.error(
                            f"too many configuration provided for fexpr {fexpr_command} "
                            f"(expected {expected_len}, got {len(benchmarks)})"
                        )

                    with ExitStack() as stack:
                        tars: list[tarfile.TarFile] = []
                        for compiler, config in benchmarks:
                            benchmark = OxcamlBenchmark(compiler, rev, config)
                            fexpr_path = benchmark.record_fexpr(jobs=fexpr_opts.jobs)
                            tars.append(stack.enter_context(tarfile.open(fexpr_path)))

                        if tars and fexpr_command == "diff":
                            tar, *tars = tars

                            compare_tars(tar, tars)

                case _:
                    parser.error(f"unknown fexpr command: {fexpr_command}")

        case _:
            parser.error(f"unknown command: {command}")


if __name__ == "__main__":
    main()
