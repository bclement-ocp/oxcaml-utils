import contextlib
import subprocess
import tempfile
import os
import json

def parse_target(rev):
    index = rev.find('/')
    if index > 0:
        user, rev = rev[:index], rev[index + 1:]
    else:
        user = 'oxcaml'

    index = rev.find('/')
    if index > 0:
        repo, rev = rev[:index], rev[index + 1:]
    else:
        repo = 'oxcaml'

    return user, repo, rev

def write_boot_ws(d, *, profile_dir):
    with open(os.path.join(d, 'duneconf', 'boot.ws'), 'w') as f:
        f.write(f'''(lang dune 2.8)
; We need to call the boot context "default" so that dune selects it for merlin
(context (default
  (name default)
  (profile main)
  (env (_
    (flags (:standard -warn-error +A -alert -unsafe_multidomain))
    (env-vars
        ("DUNE_JOBS" "1")
        ("OCAMLPARAM" "_,dump-dir={profile_dir},dump-into-csv=1,profile=1")
      )))))
''')

def configure(d, *, profile_dir):
    write_boot_ws(d, profile_dir=profile_dir)

    subprocess.call(["autoconf", "--force"], cwd=d)
    subprocess.call(["./configure", "--enable-runtime5", "--disable-optional-checks"], cwd=d)


patch = '''
diff --git a/Makefile.common-ox b/Makefile.common-ox
index dde942a3a6..64b7dbb9fe 100644
--- a/Makefile.common-ox
+++ b/Makefile.common-ox
@@ -81,7 +81,7 @@ boot_targets = \
   ocamltest/ocamltest.native
 
 boot-compiler: _build/_bootinstall
-	RUNTIME_DIR=$(RUNTIME_DIR) $(dune) build $(ws_boot) $(coverage_dune_flags) $(boot_targets)
+	RUNTIME_DIR=$(RUNTIME_DIR) $(dune) build -j 1 $(ws_boot) $(coverage_dune_flags) $(boot_targets)
 
 boot-runtest: boot-compiler
 	RUNTIME_DIR=$(RUNTIME_DIR) $(dune) runtest $(ws_boot) $(coverage_dune_flags) --force

'''

def main(repo, *, output, use_colley=False):
    try:
        os.makedirs(output, exist_ok=False)
    except FileExistsError:
        print('error: output directory already exists')
        exit(1)

    user, repo, rev = parse_target(repo)

    url = f'https://github.com/{user}/{repo}'

    with tempfile.TemporaryDirectory(prefix='oxcaml_') as d:
        code = subprocess.call(["git", "clone", "--revision", rev, "--depth", "1", url, d])
        if code != 0:
            print('error: git clone failed')
            exit(1)

        rev_parse_out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, cwd=d)
        commit = rev_parse_out.stdout.strip().decode()

        meta = {
            'user': user,
            'rev': rev,
            'commit': commit,
        }

        pd = output
        with open(os.path.join(pd, 'META.json'), 'w') as f:
            json.dump(meta, f)

        profile_dir = os.path.join(pd, 'profile')
        os.mkdir(profile_dir)

        configure(d, profile_dir=profile_dir)

        with tempfile.TemporaryFile() as fp:
            fp.write(patch.encode())
            fp.seek(0)

            code = subprocess.call(["patch", "-p1"], stdin=fp, cwd=d)
            if code != 0:
                print('error: patch failed')
                exit(1)

        env = os.environ.copy()
        env["DUNE_JOBS"] = "1"
        command = ["make", "boot-compiler"]

        if use_colley:
            command = ["colley-run", "1", "--"] + command

        subprocess.call(command, cwd=d, env=env)

        print(f'stored profiles into: {profile_dir}')

if __name__ == "__main__":
    import argparse
    import shutil

    has_colley = bool(shutil.which('colley-run'))

    parser = argparse.ArgumentParser()

    parser.add_argument('rev')
    parser.add_argument('-o', '--output', required=True)

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--use-colley', default=has_colley,
                       action=argparse.BooleanOptionalAction)

    ns = parser.parse_args()
    main(ns.rev, output=ns.output, use_colley=ns.use_colley)
