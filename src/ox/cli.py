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
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import cached_property, partial, partialmethod
from typing import ClassVar

import polars as pl
from patiencediff import PatienceSequenceMatcher

CACHE_HOME = pathlib.Path(os.getenv("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache")
CACHE_DIR = pathlib.Path(os.getenv("OX_CACHE_DIR") or CACHE_HOME.joinpath("ox"))

DEFAULT_REVISION = "5.4.0-ox8"

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
                repo = "ocaml" if user == "ocaml" else "oxcaml"
            else:
                user, repo = value[:index], value[index + 1 :]

        return cls(user, repo, rev)

    def __str__(self):
        s = "" if self.repo == "oxcaml" else f"/{self.repo}"
        s = "" if not s and self.user == "oxcaml" else f"@{self.user}{s}"
        return self.rev + s

    @property
    def shorthand(self):
        # CR bclement: this is very hackish (:
        if self.user == "ocaml" and self.repo == "ocaml":
            return self.rev.split("+")[0]

        return str(self)

    @cached_property
    def url(self):
        ocaml_version = self.ocaml_version
        if ocaml_version is None:
            archive = self.rev
        else:
            archive, *_ = ocaml_version.split("+")
        return f"https://github.com/{self.user}/{self.repo}/archive/{archive}.tar.gz"

    @property
    def ocaml_version(self):
        # CR bclement: this is very hackish (:
        if self.user == "ocaml" and self.repo == "ocaml":
            return self.rev

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
    def shorthand(self):
        return self.revision.shorthand

    @property
    def sha256(self):
        return self.revision.sha256

    def _run(self, *args, **kwargs):
        if self.revision.ocaml_version in ("5.4.1", "5.4.1+flambda"):
            flambda_support = (
                "true" if self.revision.rev.endswith("+flambda") else "false"
            )
            args = (
                "nix-shell",
                "--arg",
                "flambdaSupport",
                flambda_support,
                os.path.join(PACKAGE_DATA_ROOT, "ocaml-shell.nix"),
                *args,
            )
        else:
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
    INLINING_REPORT = 2
    CMM = 3
    RUNTIME = 4

    @property
    def requires_profiling(self):
        return self is BenchmarkMode.PROFILE or self is BenchmarkMode.RUNTIME


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

    @property
    def shorthand(self):
        info = list(self.flags)
        for key, value in self.params.items():
            if value == "1":
                info += [key]
            else:
                info += [f"{key}={value}"]
        return " ".join(info)

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

    def write_boot_ws(self, fp, *, inlining_report, dump_fexpr, dump_cmm, profile_dir):
        ocamlopt_flags = self.flags
        if dump_fexpr:
            ocamlopt_flags += ["-dfexpr-annot", "-dcanonical-ids", "-color", "never"]
        if dump_cmm:
            ocamlopt_flags += [
                "-dcmm",
                "-dump-into-file",
                "-dcanonical-ids",
                "-color never",
            ]

        ocamlparams = [f"{key}={value}" for key, value in self.params.items()]
        if profile_dir:
            # XXX: oxcaml should use dump-into-csv
            ocamlparams += [f"dump-dir={profile_dir},dump-into-file=1,profile=1"]
        if inlining_report:
            ocamlparams += ["inlining-report=1"]

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

    def write_build_sh(
        self,
        fp,
        target,
        *,
        use_colley=None,
        profile_dir,
        build_dir: pathlib.Path,
        jobs: int,
    ):
        if use_colley is None:
            use_colley = bool(shutil.which("colley-run"))

        make_cmd = f"make {target}"
        if use_colley:
            make_cmd = f"colley-run {jobs} -- {make_cmd}"

        ocamlparams: list[str] = []
        if profile_dir is not None:
            # XXX: oxcaml should use dump-into-csv
            ocamlparams += [
                f"dump-dir={profile_dir}/runtime,dump-into-file=1,profile=1"
            ]

        extra = ""
        if profile_dir:
            extra = f"""
            find {shlex.quote(os.fspath(profile_dir))} -name 'profile.*.dump' \\
                -exec sh -c 'for f do profile2csv < "$f" > "${{f/.dump/.csv}}"; done' sh {{}} \\;
            """

        fp.write(f"""#!/usr/bin/env bash
set -euo pipefail

cd {shlex.quote(os.fspath(build_dir))}

autoconf --force
./configure --enable-runtime5 --disable-optional-checks

export DUNE_JOBS={jobs}
export OCAMLPARAM={",".join(["_"] + ocamlparams)}
{make_cmd}
{extra}
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
    components = s.strip().split(",")
    components = [
        dict(zip(["counter", "value"], (s.strip() for s in component.split("="))))
        for component in components
    ]
    return components


def append_profile_csv(profiles, profile_path):
    try:
        df = pl.read_csv(profile_path)
    except pl.exceptions.NoDataError:
        return

    pass_names = df.get_column("pass name")
    if pass_names.is_empty() or pass_names[0] is None:
        return

    fname = df.get_column("pass name")[0].removeprefix("file=").rstrip("/")
    df = df.with_columns(pl.col("time").cast(pl.Float64))
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
        pl.col("alloc").cast(pl.Int64),
        pl.col("top heap").cast(pl.Int64),
        pl.col("absolute top heap").cast(pl.Int64),
    )
    df = df.with_columns(
        pl.col("counters").map_elements(
            parse_counters,
            return_dtype=pl.List(pl.Struct({"counter": pl.String, "value": pl.String})),
        )
    )
    df = df.insert_column(0, pl.lit(fname).alias("file"))
    profiles.append(df)


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
    def shorthand(self):
        return self.compiler.shorthand + " " + self.configuration.shorthand

    @property
    def base_path(self):
        return CACHE_DIR.joinpath(
            self.compiler.sha256, self.revision.sha256, self.configuration.sha256
        )

    @property
    def build_profile_path(self):
        return self.base_path / "build_profile.parquet"

    @property
    def runtime_profile_path(self):
        return self.base_path / "runtime_profile.parquet"

    @property
    def fexpr_path(self):
        return self.base_path / "fexpr.tar.gz"

    @property
    def size_path(self):
        return self.base_path / "artifact_size.csv"

    @property
    def cmm_path(self):
        return self.base_path / "cmm.tar.gz"

    @property
    def inlining_report_path(self):
        return self.base_path / "inlining_report.tar.gz"

    def _run(self, *, mode: BenchmarkMode, jobs=None):
        if not jobs or mode.requires_profiling:
            jobs = 1

        profile_dir = None
        boot_profile_dir = None
        with tempfile.TemporaryDirectory() as unpack_dir:
            if mode.requires_profiling:
                profile_dir = os.path.join(unpack_dir, "profile")
                boot_profile_dir = os.path.join(profile_dir, "build")

            build_dir = self.revision.unpack(os.path.join(unpack_dir, "source"))

            boot_ws = os.path.join(build_dir, "duneconf", "boot.ws")
            with open(boot_ws, "w") as fp:
                self.configuration.write_boot_ws(
                    fp,
                    profile_dir=boot_profile_dir,
                    dump_fexpr=mode is BenchmarkMode.FEXPR,
                    dump_cmm=mode is BenchmarkMode.CMM,
                    inlining_report=mode is BenchmarkMode.INLINING_REPORT,
                )

            build_sh = os.path.join(unpack_dir, "build.sh")
            target = "boot-compiler"
            if mode is BenchmarkMode.RUNTIME:
                target = "compiler"
            with open(build_sh, "w") as fp:
                self.configuration.write_build_sh(
                    fp,
                    target,
                    profile_dir=profile_dir,
                    build_dir=build_dir,
                    jobs=jobs,
                )

            self.compiler.run("bash", build_sh)

            if profile_dir is not None:
                self.build_profile_path.parent.mkdir(parents=True, exist_ok=True)
                build_profiles = []
                for p in pathlib.Path(profile_dir).joinpath("build").glob("**/*.csv"):
                    append_profile_csv(build_profiles, p)
                pl.concat(build_profiles).write_parquet(self.build_profile_path)
                if mode is BenchmarkMode.RUNTIME:
                    self.runtime_profile_path.parent.mkdir(parents=True, exist_ok=True)
                    runtime_profiles = []
                    for p in (
                        pathlib.Path(profile_dir).joinpath("runtime").glob("**/*.csv")
                    ):
                        append_profile_csv(runtime_profiles, p)
                    pl.concat(runtime_profiles).write_parquet(self.runtime_profile_path)

            if mode is BenchmarkMode.FEXPR:
                self.fexpr_path.parent.mkdir(parents=True, exist_ok=True)
                with tarfile.open(self.fexpr_path, "x:gz") as tar:
                    dune_dir = build_dir.joinpath("_build", "default")
                    for path in dune_dir.glob("**/*.simplify.fl"):
                        tar.add(path, "simplify" / path.relative_to(dune_dir))

                logger.info(f"Stored fexpr output in {self.fexpr_path}")

            if mode is BenchmarkMode.CMM:
                self.cmm_path.parent.mkdir(parents=True, exist_ok=True)
                with tarfile.open(self.cmm_path, "x:gz") as tar:
                    dune_dir = build_dir.joinpath("_build", "default")
                    for path in dune_dir.glob("**/*.cmx.dump"):
                        tar.add(path, "cmm" / path.relative_to(dune_dir))

                logger.info(f"Stored cmm output in {self.cmm_path}")

            if mode is BenchmarkMode.INLINING_REPORT:
                self.inlining_report_path.parent.mkdir(parents=True, exist_ok=True)
                with tarfile.open(self.inlining_report_path, "x:gz") as tar:
                    dune_dir = build_dir.joinpath("_build", "default")
                    for path in dune_dir.glob("**/*.inlining.org"):
                        tar.add(path, "inlining" / path.relative_to(dune_dir))

            self.size_path.parent.mkdir(parents=True, exist_ok=True)
            extensions = [
                "exe",
                "opt",
                "a",
                "cmxa",
                "cma",
                "cmi",
                "cmx",
                "cmo",
                "cms",
                "cmsi",
                "cmt",
                "cmti",
                "o",
            ]

            with self.size_path.open("w") as csv_file:
                csv_file.write("name,size_in_bytes\n")

                install_path = pathlib.Path(build_dir).joinpath("_build", "default")
                for ext in extensions:
                    files = list(install_path.rglob(f"*.{ext}"))
                    for file in files:
                        if file.is_file() and not file.is_symlink():
                            size = file.stat().st_size
                            relative_path = file.relative_to(install_path)
                            csv_file.write(f"{relative_path},{size}\n")

    def record_profile(self):
        logger.info("Recording profile for:")
        for line in str(self).splitlines():
            logger.info("    " + line)

        if self.build_profile_path.exists():
            logger.info(f"Using cached results from: {self.build_profile_path}")
        else:
            self._run(mode=BenchmarkMode.PROFILE)

        return (self.shorthand, self.build_profile_path)

    def record_runtime(self):
        logger.info("Recording runtime for:")
        for line in str(self).splitlines():
            logger.info("    " + line)

        if self.runtime_profile_path.exists():
            logger.info(f"Using cached results from: {self.runtime_profile_path}")
        else:
            self._run(mode=BenchmarkMode.RUNTIME)

        return (self.shorthand, self.runtime_profile_path)

    def record_fexpr(self, **kwargs):
        logger.info("Dumping fexpr for:")
        for line in str(self).splitlines():
            logger.info("    " + line)

        if self.fexpr_path.exists():
            logger.info(f"Using cached results from: {self.fexpr_path}")
        else:
            self._run(mode=BenchmarkMode.FEXPR, **kwargs)

        return self.fexpr_path

    def record_cmm(self, **kwargs):
        logger.info("Dumping cmm for:")
        for line in str(self).splitlines():
            logger.info("    " + line)

        if self.cmm_path.exists():
            logger.info(f"Using cached results from: {self.cmm_path}")
        else:
            self._run(mode=BenchmarkMode.CMM, **kwargs)

        return self.cmm_path

    def record_inlining(self, **kwargs):
        logger.info("Recording inlining report for:")
        for line in str(self).splitlines():
            logger.info("    " + line)

        if self.inlining_report_path.exists():
            logger.info(f"Using cached results from: {self.inlining_report_path}")
        else:
            self._run(mode=BenchmarkMode.INLINING_REPORT, **kwargs)

        return self.inlining_report_path

    def report_size(self, **kwargs):
        if self.size_path.exists():
            logger.info(f"Using cached results from: {self.size_path}")
        else:
            self._run(mode=BenchmarkMode.FEXPR, **kwargs)

        return self


def fix_one_pass_name(s):
    if s.startswith("/file="):
        parts = s[len("/file=") :].split("/")
        for index, part in enumerate(parts):
            if "." in part:
                break
        fname, pname = parts[: index + 1], parts[index + 1 :]
        return "/".join(pname)

    return s


def extract_file(s):
    if s["pass name"].startswith("/file="):
        parts = s["pass name"][len("/file=") :].split("/")
        for index, part in enumerate(parts):
            if "." in part:
                break
        fname, pname = parts[: index + 1], parts[index + 1 :]
        return "/".join(fname)

    return s["file"]


def fix_pass_name(df):
    df = df.with_columns(
        pl.col("pass name").map_elements(
            fix_one_pass_name, return_dtype=pl.self_dtype()
        ),
        pl.struct("file", "pass name")
        .map_elements(extract_file, return_dtype=pl.String)
        .alias("file"),
    )
    return df.filter(pl.col("file") != "load_visible_dirs")


def hierarchize_list(df_list, *, column="time"):
    df_time = pl.concat(
        [
            df.group_by("pass name").agg(pl.col(column).sum().alias(f"{column}_{i}"))
            for i, df in enumerate(df_list)
        ],
        how="align",
    ).select(
        pl.col("pass name"),
        pl.concat_list(
            [pl.coalesce(pl.col(f"{column}_{i}"), 0.0) for i in range(len(df_list))]
        ).alias(column),
    )

    data = {}
    for pass_name, time in df_time.with_columns(
        pl.col("pass name").str.split("/").list.slice(1)
    ).iter_rows():
        current = data
        for component in pass_name:
            current = current.setdefault(component, {})
        current[""] = time

    prefix = []
    while len(data) == 1:
        pass_name = list(data.keys())[0]
        prefix.append(pass_name)
        data = data[pass_name]
    return "/".join(prefix), data


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


def print_timings(
    timings,
    name="",
    *,
    depth=0,
    prefix="",
    total=None,
    max_depth=None,
    abstime=False,
    unit="s",
):
    if max_depth is not None and depth > max_depth:
        return

    scale_prefixes = ["k", "M", "G"]

    twidth = 7
    pwidth = 6

    if total is None:
        extra_prefix = ""
        total = timings.get("", [0.0])
    else:
        extra_prefix = "  | "

    line = ""
    first = True
    reference = None
    for time, total_time in zip(timings.get("", []), total):
        if not time:
            line += f"{'----':>{twidth}}  "
            if not abstime:
                line += f"{'':>{pwidth}}  "
        else:
            if reference is None or abstime:
                palign = ">"

                if not total_time:
                    percent = 100
                else:
                    percent = (time / total_time) * 100

                if first:
                    reference = time
            else:
                # Delta relative to the total time of the reference run
                palign = "+"
                percent = (time - reference) / total[0] * 100
            scale_index = 0
            scale = unit
            if unit != "s":
                while scale_index < len(scale_prefixes) and time > 1024:
                    scale = scale_prefixes[scale_index] + unit
                    time /= 1024
                    scale_index += 1
            line += f"{time:>{twidth}.02f}{scale} "
            if not abstime:
                line += f"{percent:{palign}{pwidth}.01f}% "

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
            abstime=abstime,
            unit=unit,
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


class GenericDiffer:
    # Adapted from the stdlib's difflib.Differ

    PREFIXES: ClassVar[dict[str, str]] = {
        "+": "\033[30;42m+|\033[0m\033[32m",
        "-": "\033[30;41m-|\033[0m\033[31m",
        "=": "\033[30;100m |\033[0m",
    }
    prefix_location = "\033[30;100m@|\033[0m\033[1m"

    style_reset = "\033[0m"

    context_lines = 3

    def accept(self, name):
        return True

    def normalize(self, line):
        return line

    def chunks(self, line):
        return line

    def compare(self, a, b):

        a_norm = [self.normalize(line) for line in a]
        b_norm = [self.normalize(line) for line in b]

        crunch = PatienceSequenceMatcher(None, a=a_norm, b=b_norm)
        for group in crunch.get_grouped_opcodes(self.context_lines):
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
            lambda c: isinstance(c, str) and difflib.IS_CHARACTER_JUNK(c),
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
            cruncher.set_seq2(self.chunks(b[j]))
            # Search the corresponding i's within WINDOW for rhe highest
            # ratio greater than `cutoff`.
            aequiv = alo + (j - blo)
            arange = range(max(aequiv - WINDOW, dump_i), min(aequiv + WINDOW + 1, ahi))
            if not arange:  # likely exit if `a` is shorter than `b`
                break
            best_ratio = cutoff
            for i in arange:
                cruncher.set_seq1(self.chunks(a[i]))
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
                aelt = self.chunks(aelt)
                belt = self.chunks(belt)
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


class FexprDiffer(GenericDiffer):
    def accept(self, name):
        return name.endswith(".fl")

    def normalize(self, line):
        return normalize_line(line)

    def chunks(self, line):
        return fexpr_line(line)


class CmmDiffer(GenericDiffer):
    REFERENCE_RE = re.compile(r'(G|L):"[^"]+"')
    DEFINITION_RE = re.compile('"[^"]+":')
    STAMP_RE = re.compile(r"\b([a-zA-Z_*][a-zA-Z_*0-9]*)/[0-9]+")
    CODE_ID_RE = re.compile(r"([a-zA-Z0-9_]+)_[0-9]+_[0-9]+")
    EXIT_RE = re.compile(r"(\(exit)\s+[0-9]+")
    WITH_RE = re.compile(r"(with\s*\()[0-9]+\s*")

    def normalize(self, line):
        line = self.REFERENCE_RE.sub(r"\1", line)
        line = self.DEFINITION_RE.sub(r":", line)
        line = self.STAMP_RE.sub(r"\1", line)
        line = self.CODE_ID_RE.sub(r"\1", line)
        line = self.EXIT_RE.sub(r"\1", line)
        line = self.WITH_RE.sub(r"\1", line)
        return line

    CHUNK_RE = re.compile(r"([0-9]+|[a-zA-Z][a-zA-Z0-9]+)")

    def chunks(self, line):
        return tuple(self.CHUNK_RE.split(line))

    def accept(self, name):
        return name.endswith(".cmx.dump")


INLINING_UID = re.compile(r"<<[a-f0-9]+>>")


class Group:
    def __init__(self, lines):
        self._header = INLINING_UID.sub("<<>>", lines[0])
        self.lines = lines

    def __hash__(self):
        return hash(self._header)

    def __eq__(self, other):
        if isinstance(other, Group):
            return self._header == other._header

        return NotImplemented


class InliningDiffer(GenericDiffer):
    context_lines = 30

    def _group(self, a):
        dump_i = 0
        for i in range(len(a)):
            if not a[i].startswith("*"):
                continue

            if a[dump_i:i]:
                yield Group(a[dump_i:i])
            dump_i = i

        if a[dump_i:]:
            yield Group(a[dump_i:])

    def compare(self, a, b):
        ga = list(self._group(a))
        gb = list(self._group(b))

        cruncher = difflib.SequenceMatcher(None, ga, gb, autojunk=False)

        for tag, ai1, ai2, bj1, bj2 in cruncher.get_opcodes():
            if tag == "equal":
                for la, lb in zip(ga[ai1:ai2], gb[bj1:bj2]):
                    yield from super().compare(la.lines, lb.lines)

            else:
                la = [line for g in ga[ai1:ai2] for line in g.lines]
                lb = [line for g in gb[bj1:bj2] for line in g.lines]

                yield from super().compare(la, lb)

    # No fanciness
    _fancy_replace = GenericDiffer._plain_replace

    def accept(self, name):
        return name.endswith(".inlining.org")

    DEFINED_HERE = re.compile(r"\[\[[a-f0-9]+\]\[([a-z]+)\]\]")

    def normalize(self, line):
        line = INLINING_UID.sub("<<>>", line)
        line = self.DEFINED_HERE.sub(r"[[][\1]]", line)
        return line

    SPLIT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")

    def chunks(self, line):
        return tuple(self.SPLIT_RE.split(line))


def compare_diffs(prev_str, nexts, *, differ):
    if differ is None:
        differ = GenericDiffer()

    prev_lines = prev_str.splitlines(keepends=True)

    for next_str in nexts:
        next_lines = next_str.splitlines(keepends=True)
        yield from differ.compare(prev_lines, next_lines)


def compare_tars(prev_tar: tarfile.TarFile, tars: list[tarfile.TarFile], *, differ):
    try:
        for prev_info in prev_tar.getmembers():
            name = prev_info.name
            if not prev_info.isfile() or not differ.accept(os.path.basename(name)):
                continue

            try:
                prev = prev_tar.extractfile(name).read().decode("utf-8")
                nexts = [tar.extractfile(name).read().decode("utf-8") for tar in tars]
            except UnicodeDecodeError:
                print(f"Skipping file {name} due to unicode error")

            diff_lines = list(compare_diffs(prev, nexts, differ=differ))
            if diff_lines:
                _, name = name.split(os.sep, 1)
                sys.stdout.write(f"\033[31m------\033[0m \033[1ma/{name}\033[0m\n")
                sys.stdout.write(f"\033[32m++++++\033[0m \033[1mb/{name}\033[0m\n")
                sys.stdout.writelines(diff_lines)
    except BrokenPipeError:
        pass


from copy import copy
from optparse import Option, OptionParser, Values, make_option


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


def str_bytes(b):
    for unit in ["B", "kB", "MB", "GB"]:
        if b < 1024:
            break

        b /= 1024

    return f"{b:.01f}{unit}"


def process_size(s):
    reference = s.drop_nulls().max()
    scale = 1
    for unit in ["B", "kB", "MB", "GB"]:
        if reference < 1024:
            break

        scale *= 1024
        reference /= 1024

    return pl.struct(
        (s / scale).round_sig_figs(3).alias(f"{s.name}"),
        pl.lit(unit).alias(f"{s.name} unit"),
        (s / s.drop_nulls().first()).round(1).alias(f"{s.name} (rel)"),
    )


def process_time(s):
    return pl.struct(
        s.round_sig_figs(3).alias(f"{s.name} (s)"),
        (s / s.drop_nulls().first()).round(1).alias(f"{s.name} (rel)"),
    )


class Cli:
    parser: ClassVar[OptionParser] = OptionParser(
        description="An OxCaml revision manager",
        usage="%prog [options] command ...",
        option_class=OxOption,
    )
    parser.add_option(
        "-r",
        "--revision",
        dest="rev",
        default=os.getenv("OXCAML_REVISION") or DEFAULT_REVISION,
        type="revision",
    )

    def _run(self, name, args=None, opts=None, *, dispatch):
        parser = getattr(
            self,
            f"parser{name.replace(' ', '_')}",
            OptionParser(usage=f"%prog{name.rstrip()}"),
        )
        parser.disable_interspersed_args()
        opts_, args_ = parser.parse_args(args)

        # Accumulate options from the command hierarchy
        if opts is None:
            opts = Values()
        for attr, val in opts_.__dict__.items():
            setattr(opts, attr, val)

        dispatch(name, parser, opts, args_)

    def _dispatch(self, name, parser: OptionParser, opts: Values, args: list[str]):
        if not args:
            parser.error("missing argument")

        command, *args = args

        if "_" in command:
            parser.error("invalid command")

        dispatch = getattr(self, f"run{name.replace(' ', '_')}_{command}", None)
        if dispatch is None:
            parser.error("invalid command")

        self._run(f"{name} {command}", args, opts, dispatch=dispatch)

    def _record(self, name, parser, opts, args, *, task, n=None):
        benchmark_parser = OptionParser(usage=f"%prog{name}", option_class=OxOption)
        benchmark_parser.add_option("-P", action="append", dest="params", type="param")
        benchmark_parser.add_option("-F", action="append", dest="flags")
        benchmark_parser.disable_interspersed_args()

        benchmarks = []
        while args and (n is None or len(benchmarks) < n):
            compiler = OxcamlCompiler(OxcamlRevision.parse(args[0]))
            bopts, args = benchmark_parser.parse_args(args[1:])
            config = OxcamlConfiguration(dict(bopts.params or {}), bopts.flags or [])
            benchmarks.append(OxcamlBenchmark(compiler, opts.rev, config))

        if n is not None:
            if len(benchmarks) != n:
                parser.error(f"not enough configurations (expected {n})")

            if args:
                parser.error(f"too many configurations (expected {n})")

        if not benchmarks:
            parser.error("expected compiler configuration(s)")

        return [task(benchmark) for benchmark in benchmarks]

    def _report(self, name, parser, opts, args, *, task):
        profiles = self._record(name, parser, opts, args, task=task)

        df_list = []
        for _, profile in profiles:
            df = pl.read_parquet(profile)

            df_list.append(df)

        max_depth = opts.max_depth

        column = opts.column or "time"
        unit = "s" if column == "time" else "B"

        if opts.top_files is None:
            name, timings = hierarchize_list(df_list, column=column)
            print_timings(
                timings, name=name, max_depth=max_depth, unit=unit, abstime=False
            )
        else:
            files = (
                pl.concat(
                    [
                        df.filter(pl.col("pass name").str.contains(pass_pat))
                        .group_by("file")
                        .agg(pl.col("time").sum().alias(f"time_{i}"))
                        for i, df in enumerate(df_list)
                    ],
                    how="align_left",
                )
                .select(
                    pl.col("file"),
                    pl.concat_list([f"time_{i}" for i in range(len(df_list))]).alias(
                        "time"
                    ),
                )
                .sort("time", descending=True)
                .head(opts.top_files)["file"]
            )

            for f in files:
                print(f)
                name, timings = hierarchize_list(
                    [df.filter(pl.col("file") == f) for df in df_list]
                )
                print_timings(timings, name=name, max_depth=max_depth)

        df_list = []
        for configuration, profile in profiles:
            df = pl.read_parquet(profile)

            df = df.with_columns(pl.col("pass name").str.strip_chars_start("/"))

            df = df.group_by("pass name").agg(
                pl.col("time").sum(), pl.col("alloc").sum()
            )

            df = df.select(
                pl.lit(configuration).alias("configuration"),
                pl.col("pass name"),
                pl.col("time"),
                pl.col("alloc"),
            )

            df_list.append(df)

        df = pl.concat(df_list)

        cols = [pl.col(""), pl.col("configuration")]
        if opts.filter_pass:
            cols += [
                pl.coalesce(pl.col(pass_name), pl.lit(None)).alias(pass_name)
                for pass_name in opts.filter_pass
            ]

        df_time = (
            df.pivot("pass name", index="configuration", values="time")
            .select(cols)
            .map_columns(
                pl.selectors.exclude("configuration"),
                lambda s: process_time(s.alias("time")),
            )
            .unpivot(
                None,
                index="configuration",
                variable_name="pass name",
                value_name="time",
            )
        )
        df_alloc = (
            df.pivot("pass name", index="configuration", values="alloc")
            .select(cols)
            .map_columns(
                pl.selectors.exclude("configuration"),
                lambda s: process_size(s.alias("alloc")),
            )
            .unpivot(
                None,
                index="configuration",
                variable_name="pass name",
                value_name="alloc",
            )
        )
        df = df_time.join(df_alloc, on=["configuration", "pass name"])
        df = (
            df.select("pass name", "configuration", "time", "alloc")
            .unnest("time", "alloc")
            .with_columns(pl.format("{}{}", "alloc", "alloc unit").alias("alloc"))
            .select(pl.all().exclude("alloc unit"))
        )

        with pl.Config(
            tbl_hide_dataframe_shape=True,
            tbl_hide_column_data_types=True,
            tbl_formatting="ASCII_MARKDOWN",
            tbl_rows=-1,
            tbl_cols=-1,
            tbl_cell_numeric_alignment="RIGHT",
        ):
            for (pass_name,), pass_df in df.group_by("pass name", maintain_order=True):
                print(f"## {pass_name}")
                print()
                print(pass_df.select(pl.all().exclude("pass name")))
                print()

    def _dump(self, name, parser, opts, args, *, task, n=None):
        return self._record(
            name, parser, opts, args, task=partial(task, jobs=opts.jobs), n=n
        )

    def _diff(self, name, parser, opts, args, *, task, differ):
        paths = self._dump(name, parser, opts, args, task=task, n=2)

        if len(paths) != 2:
            parser.error(
                f"too many configuration provided for {name}"
                f"(expected 2, got {len(paths)})"
            )

        with ExitStack() as stack:
            tars: list[tarfile.TarFile] = []
            for path in paths:
                tars.append(stack.enter_context(tarfile.open(path)))

            if tars:
                tar, *tars = tars

                compare_tars(tar, tars, differ=differ)

    def _export(self, name, parser, opts, args, *, task):
        if not args:
            parser.error(f"{name} DST REV ...")

        output, *args = args
        (path,) = self._dump(name, parser, opts, args, task=task, n=1)

        def filter(member: tarfile.TarInfo, path):
            if opts.glob is None or pathlib.PurePath(member.name).match(opts.glob):
                logger.info(f"Extracting {member.name}")
                return tarfile.data_filter(member, path)

        with tarfile.open(path) as tar:
            tar.extractall(output, filter=filter)

    def run(self):
        self._run("", dispatch=self._dispatch)

    def run_ocamlopt(self, _name, _parser, opts: Values, args: list[str]):
        OxcamlCompiler(opts.rev).run("ocamlopt", args)

    run_opt = run_ocamlopt

    def run_shell(self, name, parser, opts: Values, args: list[str]):
        if args:
            parser.error(f"unexpected arguments for {name} command")

        OxcamlCompiler(opts.rev).shell()

    parser_profile_report = OptionParser(usage="%prog profile report [options] ...")
    parser_profile_report.add_option(
        "-p", dest="filter_pass", action="append", help="filter pass name"
    )
    parser_profile_report.add_option(
        "-n", dest="top_files", help="only display top N files", type=int
    )
    parser_profile_report.add_option(
        "-d", dest="max_depth", help="max depth of passes to print", type=int
    )
    parser_profile_report.add_option(
        "-c", dest="column", help="column to print", type=str
    )

    run_profile = _dispatch
    run_profile_record = partialmethod(_record, task=OxcamlBenchmark.record_profile)
    run_profile_report = partialmethod(_report, task=OxcamlBenchmark.record_profile)

    parser_runtime_report = OptionParser(usage="%prog runtime report [options] ...")
    parser_runtime_report.add_option(
        "-p", dest="filter_pass", action="append", help="filter pass name"
    )
    parser_runtime_report.add_option(
        "-n", dest="top_files", help="only display top N files", type=int
    )
    parser_runtime_report.add_option(
        "-d", dest="max_depth", help="max depth of passes to print", type=int
    )
    parser_runtime_report.add_option(
        "-c", dest="column", help="column to print", type=str
    )

    run_runtime = _dispatch
    run_runtime_record = partialmethod(_record, task=OxcamlBenchmark.record_runtime)
    run_runtime_report = partialmethod(_report, task=OxcamlBenchmark.record_runtime)

    parser_fexpr = OptionParser(usage="%prog fexpr [options] ...")
    parser_fexpr.add_option("-j", "--jobs", type=int, dest="jobs")

    parser_fexpr_export = OptionParser(usage="%prog fexpr export [options] ...")
    parser_fexpr_export.add_option(
        "-g",
        dest="glob",
        help="only export files matching this pattern",
    )

    run_fexpr = _dispatch
    run_fexpr_dump = partialmethod(_dump, task=OxcamlBenchmark.record_fexpr)
    run_fexpr_diff = partialmethod(
        _diff, task=OxcamlBenchmark.record_fexpr, differ=FexprDiffer()
    )
    run_fexpr_export = partialmethod(_export, task=OxcamlBenchmark.record_fexpr)

    parser_inlining = OptionParser(usage="%prog inlining [options] ...")
    parser_inlining.add_option("-j", "--jobs", type=int, dest="jobs")

    parser_inlining_export = OptionParser(usage="%prog inlining export [options] ...")
    parser_inlining_export.add_option(
        "-g",
        dest="glob",
        help="only export files matching this pattern",
    )

    run_inlining = _dispatch
    run_inlining_dump = partialmethod(_dump, task=OxcamlBenchmark.record_inlining)
    run_inlining_diff = partialmethod(
        _diff, task=OxcamlBenchmark.record_inlining, differ=InliningDiffer()
    )
    run_inlining_export = partialmethod(_export, task=OxcamlBenchmark.record_inlining)

    parser_cmm = OptionParser(usage="%prog cmm [options] ...")
    parser_cmm.add_option("-j", "--jobs", type=int, dest="jobs")

    parser_cmm_export = OptionParser(usage="%prog cmm export [options] ...")
    parser_cmm_export.add_option(
        "-g",
        dest="glob",
        help="only export files matching this pattern",
    )

    run_cmm = _dispatch
    run_cmm_dump = partialmethod(_dump, task=OxcamlBenchmark.record_cmm)
    run_cmm_diff = partialmethod(
        _diff, task=OxcamlBenchmark.record_cmm, differ=CmmDiffer()
    )
    run_cmm_export = partialmethod(_export, task=OxcamlBenchmark.record_cmm)

    parser_size = OptionParser(usage="%prog size [options] ...")
    parser_size.add_option("-j", "--jobs", type=int, dest="jobs")

    run_size = _dispatch

    def run_size_report(self, name, parser, opts, args):
        benchmarks = self._record(
            name, parser, opts, args, task=OxcamlBenchmark.report_size
        )

        df_list = []
        for i, benchmark in enumerate(benchmarks):
            path = benchmark.size_path
            df_list.append(
                pl.read_csv(path)
                .with_columns(
                    pl.col("name").alias("file"),
                    pl.col("name").str.split(".").list.last().alias("ext"),
                )
                .with_columns(pl.format(".{}", "ext").alias("ext"))
                .group_by("ext")
                .agg(pl.col("size_in_bytes").sum().alias(benchmark.shorthand))
            )
        df = pl.concat(df_list, how="align_left")

        with pl.Config(
            tbl_hide_dataframe_shape=True,
            tbl_hide_column_data_types=True,
            tbl_formatting="ASCII_MARKDOWN",
            tbl_rows=-1,
            tbl_cols=-1,
            tbl_cell_numeric_alignment="RIGHT",
        ):
            df = df.transpose(
                include_header=True, header_name="configuration", column_names="ext"
            )
            df = df.map_columns(
                pl.selectors.exclude("configuration"),
                process_size,
                # lambda s: pl.struct(
                #     s.map_elements(str_bytes, return_dtype=pl.String),
                #     (s / s.drop_nulls().first()).round(1).alias(f"{s.name} (rel)"),
                # ),
            ).unnest(pl.selectors.exclude("configuration"))
            print(df)


def main():
    logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

    Cli().run()


if __name__ == "__main__":
    main()
