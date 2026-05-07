{ url
, hash
, version
, pkgs ? import <nixpkgs> { }
}:

let
  makeOxcaml = ocaml:
    pkgs.ocamlPackages.callPackage (import ./oxcaml.nix {
          inherit version url hash;
        }) ({
          ocamlPackages = pkgs.ocaml-ng.ocamlPackages_4_14;
          optionalChecks = false;

          inherit ocaml;
        });

  oxcaml = makeOxcaml pkgs.ocaml-ng.ocamlPackages_5_4.ocaml;
in

pkgs.mkShell {
    name = "oxcaml-shell";
    inputsFrom = [ (makeOxcaml oxcaml) ];
}
