{ url
, hash
, version
, pkgs ? import (builtins.fetchTarball {
  name = "nixpkgs-d3498f786f97ac0bded21b34bae0bf3809b45aa3";
  url = "https://github.com/NixOS/nixpkgs/archive/d3498f786f97ac0bded21b34bae0bf3809b45aa3.tar.gz";
  sha256 = "0f9jiiqyd9g8k5h87jw6lskj6wrzldc5lc1bmxb6dk6acp39cssr";
}) {}
}:

let
  ocamlPackages = pkgs.ocaml-ng.ocamlPackages_5_4.overrideScope (self: super: {
      dune_3 = super.dune_3.overrideAttrs rec {
        version = "3.23.1";
        src = pkgs.fetchurl {
          url = "https://github.com/ocaml/dune/releases/download/${version}/dune-${version}.tbz";
          hash = "sha256-k7TnFX9rqP62HPxfhgCO/SxZA3unigF9krSr8wYyNI8=";
        };
      };
  });

  makeOxcaml = ocaml:
    ocamlPackages.callPackage (import ./oxcaml.nix {
          inherit version url hash;
        }) ({
          optionalChecks = false;

          inherit ocaml ocamlPackages;
        });

  oxcaml = makeOxcaml pkgs.ocaml-ng.ocamlPackages_5_4.ocaml;
in

pkgs.mkShell {
    name = "oxcaml-shell";
    inputsFrom = [ (makeOxcaml oxcaml) ];
}
