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
