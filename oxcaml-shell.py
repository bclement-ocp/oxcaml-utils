#!/usr/bin/env python3

import subprocess
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

def make_url(user, repo, rev):
    return f'https://github.com/{user}/{repo}/archive/{rev}.tar.gz'

def main(target):
    user, repo, rev = parse_target(target)
    url = make_url(user, repo, rev)

    out = subprocess.run(["nix-prefetch-url", url], capture_output=True)
    nixhash = out.stdout.strip().decode()

    subprocess.call([
        "nix-shell",
        "--argstr", "url", url,
        "--argstr", "hash", f"sha256:{nixhash}",
        "--argstr", "version", rev,
        "oxcaml-shell.nix"
    ])

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('target')

    ns = parser.parse_args()
    main(target=ns.target)
