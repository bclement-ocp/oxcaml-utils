{ flambdaSupport ? false
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
          # CR bclement: refactor oxcaml.nix so that we don't need these.
          version = "fake-version";
          url = "fake-url";
          hash = pkgs.lib.fakeHash;
        }) ({
          optionalChecks = false;

          inherit ocaml ocamlPackages;
        });

  ocaml = pkgs.ocaml-ng.ocamlPackages_5_4.ocaml.override { inherit flambdaSupport; };
  profile2csv = ocamlPackages.buildDunePackage {
    pname = "profile2csv";
    version = "0.1";
    src = ./vendor/profile2csv;
  };
in

pkgs.mkShell {
    name = "oxcaml-shell";
    nativeBuildInputs = [ profile2csv ];
    inputsFrom = [ (makeOxcaml ocaml) ];
}
