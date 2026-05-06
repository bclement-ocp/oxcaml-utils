{ url
, hash
, version
, pkgs ? import <nixpkgs> { }
}:

let
  ocaml_5_4_0 =
    pkgs.callPackage (
    import (pkgs.path + "/pkgs/development/compilers/ocaml/generic.nix") {
      major_version = "5";
      minor_version = "4";
      patch_version = "0";
      sha256 = "sha256-36qKLhHHmbwXZdi+9EkRQG7l9IAwJxkDgqk5+IyRImY=";
    }) { };

  makeOxcaml = ocaml:
    pkgs.ocamlPackages.callPackage (import ./oxcaml.nix {
          inherit version url hash;
        }) ({
          ocamlPackages = pkgs.ocaml-ng.ocamlPackages_4_14;
          optionalChecks = false;

          inherit ocaml;
        });

  oxcaml = makeOxcaml ocaml_5_4_0;
in

pkgs.mkShell {
    name = "oxcaml-shell";
    inputsFrom = [ (makeOxcaml oxcaml) ];
}
