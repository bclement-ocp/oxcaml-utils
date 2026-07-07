{ url
, hash
, version
, pkgs ? import <nixpkgs> { }
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
