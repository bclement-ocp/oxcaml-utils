A collection of utilities and scripts to build and benchmark OxCaml. Work in
progress.

# oxcaml-shell.py

This script allows to setup a shell ready to build OxCaml with another version
of OxCaml as the system compiler (note: you will need to pass
`--disable-optional-checks` when running `./configure`).

To build and enter a shell where the system compiler is release 5.2.0minus-37
from the `oxcaml/oxcaml` GitHub repo:

```console
$ python3 oxcaml-shell.py 5.2.0minus-37
```

To build and enter a shell where the system compiler comes from a specific
branch in a fork:

```console
$ python3 oxcaml-shell.py my-username/my-custom-tag
```

If the repository is not called `oxcaml` it must be specified:

```console
$ python3 oxcaml-shell.py my-username/flambda-backend/my-custom-tag
```

The syntax is either `user/repo/rev`, `user/rev` (equivalent to
`user/oxcaml/rev`), or `rev` (equivalent to `oxcaml/oxcaml/rev`).

# bench.py

This script (intended to be run from inside a shell created with
`oxcaml-shell.py`) allows to benchmark the build of a specific version of the
oxcaml compiler using whatever compiler is currently available in the
environment.

It can be used as:

```console
$ python3 bench.py oxcaml/main -o path/to/where/output/is
```

which will collect profile information (profile csvs) in the provided output directory.

OCAMLPARAM and compilation flags can be passed using `-p` and `-f`:

```console
$ python3 bench.py oxcaml/main -o path/to/where/output/is \
    -p my-param=my-value -f-my-flag -f my-flag-value
```
